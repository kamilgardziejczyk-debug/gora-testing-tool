import argparse
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from parser import DutCliConfig, DutLogConfig, Parser, UsbHubConfig
from reporting import TestResult, generate_report
from tools.dut_cli import DEFAULT_BAUD as DEFAULT_CLI_BAUD
from tools.dut_cli import DutShell
from tools.dut_logger import DEFAULT_BAUD as DEFAULT_DUT_BAUD
from tools.dut_logger import (
    CLI_LOG,
    DEVICE_LOG,
    MQTT_LOG,
    DutLogger,
    LogSession,
    attach as attach_log_handler,
)
from tools.usb_hub import Switchboard, UsbHub
from wrappers import (
    Wrapper,
    dut_storage_restore_all,
    mqtt_registry,
    relay_cleanup_all,
    usb_switch_restore_all,
)


LOGGER = logging.getLogger(__name__)

DEFAULT_REPORT_DIR = Path("results")


def parse_args() -> argparse.Namespace:
    argument_parser = argparse.ArgumentParser(description="Gora testing tool")
    argument_parser.add_argument(
        "-t",
        "--test",
        required=True,
        help="Path to YAML test scenario file",
    )
    argument_parser.add_argument(
        "-p",
        "--port",
        required=False,
        default=None,
        help="Serial port for flashing (e.g. /dev/ttyUSB0). Overrides the port set in the YAML scenario.",
    )
    argument_parser.add_argument(
        "-f",
        "--firmware",
        required=False,
        default=None,
        help="Path to directory containing firmware binaries (bootloader, partition table, firmware, hex, elf). Overrides the directory for all ProgramEsptool and ProgramJlink commands.",
    )
    argument_parser.add_argument(
        "-r",
        "--report",
        required=False,
        default=None,
        help="Path to write the HTML test report to, or a directory to write a default-named "
        "report into. Defaults to results/<scenario>_<timestamp>.html.",
    )
    argument_parser.add_argument(
        "--clean-results",
        action="store_true",
        help="Empty the report directory before this run, so an unattended node's results/ "
        "does not accumulate old reports, logs and !DutStorage copies forever. Off by default "
        "- local/interactive use generally wants to keep run history.",
    )
    argument_parser.add_argument(
        "--dut-log",
        required=False,
        default=None,
        help="Serial port carrying the DUT's console (e.g. /dev/ttyACM0), captured for the "
        "whole run. Overrides the scenario's own 'dut_log' block.",
    )
    argument_parser.add_argument(
        "--dut-log-baud",
        required=False,
        type=int,
        default=None,
        help=f"Baud rate for --dut-log. Defaults to the scenario's value, else {DEFAULT_DUT_BAUD}.",
    )
    argument_parser.add_argument(
        "--dut-cli",
        required=False,
        default=None,
        help="Serial port carrying the DUT's shell (e.g. /dev/ttyACM1), used by !DutCli "
        "commands. Overrides the scenario's own 'dut_cli' block.",
    )
    argument_parser.add_argument(
        "--dut-cli-baud",
        required=False,
        type=int,
        default=None,
        help=f"Baud rate for --dut-cli. Defaults to the scenario's value, else {DEFAULT_CLI_BAUD}.",
    )
    return argument_parser.parse_args()


def load_scenario(
    test_file: str,
) -> tuple[list[Wrapper], str | None, DutLogConfig | None, DutCliConfig | None, UsbHubConfig | None]:
    """Validate and parse a scenario into its commands, name and bench settings.

    All five come from one Parser so the file is read (and composed) only once.
    """
    parser = Parser(test_file)
    if not parser.validate():
        LOGGER.error("Invalid YAML file: %s", test_file)
        raise ValueError("Passed test file is not a valid YAML file")
    LOGGER.info("YAML validation successful")
    scenario_name = parser.parse_name()
    dut_log_config = parser.parse_dut_log()
    dut_cli_config = parser.parse_dut_cli()
    usb_hub_config = parser.parse_usb_hub()
    wrappers = parser.parse()
    LOGGER.info("Scenario parsing finished, executing %d commands", len(wrappers))
    return wrappers, scenario_name, dut_log_config, dut_cli_config, usb_hub_config


def _merge_serial_config(
    scenario_config: DutLogConfig | DutCliConfig | None,
    port_arg: str | None,
    baud_arg: int | None,
    default_baud: int,
    label: str,
) -> tuple[str, int] | None:
    """Merge a scenario's serial block with its CLI overrides, or None if neither.

    The CLI port wins over the scenario's, matching how `--port` and
    `--firmware` already override their YAML equivalents: which device path a
    UART has is a property of the test *node*, not of the test. The baud
    override can be given on its own, to re-rate a port the scenario declared.
    """
    port = port_arg or (scenario_config.port if scenario_config else None)
    if port is None:
        return None

    baud = baud_arg or (scenario_config.baud if scenario_config else None) or default_baud
    if port_arg is not None and scenario_config is not None and port_arg != scenario_config.port:
        LOGGER.info("Overriding scenario %s port %s with CLI value: %s", label, scenario_config.port, port_arg)
    return port, baud


def resolve_dut_log(
    scenario_config: DutLogConfig | None,
    port_arg: str | None,
    baud_arg: int | None,
) -> DutLogConfig | None:
    """Merge the scenario's `dut_log` block with `--dut-log`/`--dut-log-baud`."""
    merged = _merge_serial_config(scenario_config, port_arg, baud_arg, DEFAULT_DUT_BAUD, "DUT log")
    return None if merged is None else DutLogConfig(*merged)


def resolve_dut_cli(
    scenario_config: DutCliConfig | None,
    port_arg: str | None,
    baud_arg: int | None,
) -> DutCliConfig | None:
    """Merge the scenario's `dut_cli` block with `--dut-cli`/`--dut-cli-baud`."""
    merged = _merge_serial_config(scenario_config, port_arg, baud_arg, DEFAULT_CLI_BAUD, "DUT CLI")
    return None if merged is None else DutCliConfig(*merged)


def apply_cli_overrides(wrappers: list[Wrapper], port: str | None, firmware: str | None) -> None:
    for wrapper in wrappers:
        if port is not None and wrapper.supports_port_override:
            LOGGER.info("Overriding %s serial port with CLI value: %s", wrapper.tag, port)
            wrapper.port = port
        if firmware is not None and wrapper.supports_firmware_dir_override:
            LOGGER.info("Overriding %s firmware directory with CLI value: %s", wrapper.tag, firmware)
            wrapper.firmware_dir = firmware


def attach_dut_log_session(wrappers: list[Wrapper], session: LogSession | None) -> None:
    """Give commands that need the DUT's capture access to it.

    `session` is None when no DUT console is being captured. A scenario that
    asks for such a check anyway is rejected here rather than at the moment the
    check runs: it cannot pass, and failing before the first command means
    finding out before a flash and a full BLE provisioning cycle, not after.
    """
    needing_dut_log = [wrapper for wrapper in wrappers if wrapper.requires_dut_log]
    if not needing_dut_log:
        return

    if session is None:
        tags = ", ".join(sorted({f"!{wrapper.tag}" for wrapper in needing_dut_log}))
        raise ValueError(
            f"This scenario has {len(needing_dut_log)} command(s) needing the DUT console "
            f"({tags}), but no console is being captured. Pass --dut-log <port> "
            f"(e.g. /dev/ttyACM0), or add a top-level 'dut_log' block to the scenario."
        )

    for wrapper in needing_dut_log:
        wrapper.log_session = session


def attach_dut_logger(wrappers: list[Wrapper], dut_logger: DutLogger | None) -> None:
    """Give commands that suspend or resume capture the reader itself.

    Separate from `attach_dut_log_session`: reading what was captured needs the
    session, while handing the port to a programmer needs the reader holding it.
    A missing console is already rejected there, since every wrapper wanting the
    reader also declares `requires_dut_log`.
    """
    if dut_logger is None:
        return
    for wrapper in wrappers:
        if wrapper.controls_dut_log:
            wrapper.dut_logger = dut_logger


def attach_dut_cli_shell(
    wrappers: list[Wrapper],
    config: DutCliConfig | None,
    session: LogSession,
) -> DutShell | None:
    """Give !DutCli commands the run's shared shell, or reject the scenario.

    Returns the shell so the caller can close it, or None when the scenario
    sends no shell commands. Like `attach_dut_log_session`, a scenario that
    needs one without a port configured is rejected before the first command
    rather than at the moment that command runs.

    The shell is created but *not* opened here: a scenario that flashes the DUT
    first has nothing answering on the shell UART until that has happened, so
    the first !DutCli opens it (see `DutCliWrapper._require_shell`).
    """
    needing_shell = [wrapper for wrapper in wrappers if wrapper.requires_dut_cli]
    if not needing_shell:
        return None

    if config is None:
        tags = ", ".join(sorted({f"!{wrapper.tag}" for wrapper in needing_shell}))
        raise ValueError(
            f"This scenario has {len(needing_shell)} command(s) driving the DUT's shell "
            f"({tags}), but no shell UART is configured. Pass --dut-cli <port> "
            f"(e.g. /dev/ttyACM1), or add a top-level 'dut_cli' block to the scenario."
        )

    shell = DutShell(port=config.port, baud=config.baud, log_session=session)
    for wrapper in needing_shell:
        wrapper.dut_shell = shell
    session.mark_used(CLI_LOG)
    LOGGER.info("DUT shell configured on %s at %d baud", config.port, config.baud)
    return shell


def attach_usb_switchboard(
    wrappers: list[Wrapper],
    config: UsbHubConfig | None,
) -> Switchboard | None:
    """Give !UsbSwitch commands the run's hub, or reject the scenario.

    Returns the switchboard so the runner can restore power through it, or None
    when the scenario switches no USB ports. Unlike `attach_dut_cli_shell`, a
    scenario with no `usb_hub` block is still runnable - the hub is discovered
    and ports addressed by number - so the block's absence is only an error for
    a command that actually uses a name.

    Every port name is resolved here rather than at execute time, so a typo in
    one is reported before the bench is touched instead of half way through a
    run that has already flashed the DUT.
    """
    needing_hub = [wrapper for wrapper in wrappers if wrapper.requires_usb_hub]
    if not needing_hub:
        return None

    location = config.location if config else None
    switchboard = Switchboard(UsbHub(location=location), config.ports if config else None)
    for wrapper in needing_hub:
        wrapper.usb_switchboard = switchboard
        _validate_usb_port(wrapper, switchboard)

    LOGGER.info(
        "USB hub configured (%s), port names: %s",
        location or "discovered on first use",
        ", ".join(f"{name}={port}" for name, port in sorted(switchboard.ports.items())) or "none",
    )
    return switchboard


def _validate_usb_port(wrapper: Wrapper, switchboard: Switchboard) -> None:
    """Reject a !UsbSwitch naming a port that no number or alias resolves to."""
    target = getattr(wrapper, "usb_port", None)
    if target is None:
        return
    try:
        switchboard.resolve(target)
    except ValueError as error:
        raise ValueError(f"{_describe(wrapper)}: {error}") from None


def validate_dut_log_handover(
    wrappers: list[Wrapper],
    dut_log: DutLogConfig | None,
    dut_cli: DutCliConfig | None,
) -> None:
    """Reject a scenario whose console capture would fight over its own port.

    A board whose console *is* its programming port has one wire and two
    claimants, and nothing stops them physically: the port stays openable
    throughout a flash, so both simply read it and split the bytes between them.
    That shows up as an esptool handshake failing for no visible reason, which
    is why it is caught here - statically, before the first relay clicks -
    rather than left to fail differently on every run.

    Reads as a walk over the commands in order, tracking whether capture is
    running at each one, since that is exactly how the scenario's author reads
    it too.
    """
    if dut_log is None:
        return

    if dut_cli is not None and dut_cli.port == dut_log.port:
        raise ValueError(
            f"The DUT console and the DUT shell are both configured on {dut_log.port}. "
            f"One process reading a port is what makes framing a shell response possible "
            f"at all, so these must be different ports."
        )

    capturing = True
    for index, wrapper in enumerate(wrappers, start=1):
        if wrapper.controls_dut_log:
            capturing = bool(getattr(wrapper, "state", False))
            continue
        if capturing:
            _reject_port_conflict(wrapper, index, dut_log)
        elif wrapper.requires_dut_log:
            raise ValueError(
                f"Command {index} ({_describe(wrapper)}) needs the DUT console, but capture "
                f"is stopped at that point in the scenario. Resume it with a !DutLogControl "
                f"'state: 1' command first."
            )


def _reject_port_conflict(wrapper: Wrapper, index: int, dut_log: DutLogConfig) -> None:
    """Raise if `wrapper` claims the port the console capture is holding."""
    # getattr because only some tags have a serial port at all, and !MqttSubscribe
    # has a `port` that is a TCP port number rather than a device path.
    port = getattr(wrapper, "port", None)
    if not isinstance(port, str) or port != dut_log.port:
        return
    raise ValueError(
        f"Command {index} ({_describe(wrapper)}) opens {port}, which the DUT console "
        f"capture is holding at that point in the scenario. Two readers on one port split "
        f"the bytes between them, so this would corrupt both. Bracket the command with "
        f"!DutLogControl 'state: 0' before it and 'state: 1' after it."
    )


def _describe(wrapper: Wrapper) -> str:
    """A command's name and tag, for an error a scenario author can act on."""
    tag = f"!{wrapper.tag}" if wrapper.tag else type(wrapper).__name__
    name = getattr(wrapper, "name", None)
    return f'{tag} "{name}"' if name else tag


def attach_mqtt_log_session(wrappers: list[Wrapper], session: LogSession) -> None:
    """Give commands that open MQTT sessions somewhere to log their traffic.

    Unconditional, unlike `attach_dut_log_session`: the MQTT log needs no
    hardware attached, so a scenario opening a broker session always gets one.

    A scenario with no such command leaves the MQTT log unmarked, and it is
    then left out of the report rather than linked as an empty file.
    """
    capturing = [wrapper for wrapper in wrappers if wrapper.captures_mqtt_log]
    for wrapper in capturing:
        wrapper.log_session = session
    if capturing:
        session.mark_used(MQTT_LOG)


def clean_results_dir(directory: Path) -> None:
    """Empty `directory` of everything in it, leaving the directory itself.

    For `--clean-results`, on an unattended node where nothing else ever
    clears out old reports: a self-hosted runner container's results/ is a
    host bind mount that otherwise grows without bound across every job run,
    and stale !DutStorage copies sitting under it are worse than merely
    large - a `copy_from` with `dirs_exist_ok=True` would merge a new run's
    files into whatever an old one already left under the same session name.

    Not `shutil.rmtree(directory)` followed by `mkdir`: `directory` is where
    `resolve_report_path` decided the report belongs, and on a bind mount
    removing the mounted directory itself (rather than its contents) is the
    kind of thing worth not doing from inside a container. A missing
    directory is not an error - the first run on a fresh node has nothing to
    clean yet, and `LogSession.open()` creates it either way.
    """
    if not directory.exists():
        return

    removed = 0
    for entry in directory.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        removed += 1

    LOGGER.info("--clean-results: removed %d item(s) from %s", removed, directory)


def resolve_report_path(test_file: str, report_arg: str | None) -> Path:
    """The default report path is derived from the scenario name and the
    current time, so repeated runs of the same scenario don't overwrite
    each other's reports unless the user asks for a specific path.

    A `--report` value is used as the exact output file only if it already
    looks like one (an existing file, or a name with a suffix). Anything else
    - an existing directory, a path ending in a separator, or a bare name with
    no suffix like "reports" - is treated as a directory to drop the
    default-named report into, rather than silently creating an extensionless
    file with that exact name.
    """
    stem = Path(test_file).stem
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_name = f"{stem}_{timestamp}.html"

    if report_arg is None:
        return DEFAULT_REPORT_DIR / default_name

    report_path = Path(report_arg)
    looks_like_directory = (
        report_arg.endswith(("/", os.sep))
        or report_path.is_dir()
        or (not report_path.is_file() and report_path.suffix == "")
    )
    return report_path / default_name if looks_like_directory else report_path


def run_wrapper(wrapper: Wrapper) -> tuple[TestResult, Exception | None]:
    """Execute one wrapper, always producing a TestResult regardless of outcome.

    The exception (if any) is returned alongside the result rather than
    re-raised here, so the caller can record the failure in the report before
    deciding whether to stop the scenario.
    """
    start = time.monotonic()
    error: Exception | None = None
    try:
        wrapper.execute()
    except Exception as exc:  # noqa: BLE001 - captured for the report, re-raised by the caller
        error = exc
    duration_s = time.monotonic() - start

    result = TestResult(
        name=wrapper.name if getattr(wrapper, "name", None) else (wrapper.tag or type(wrapper).__name__),
        tag=wrapper.tag or type(wrapper).__name__,
        raw_yaml=wrapper.raw_yaml or "",
        validation_expected=wrapper.validation_expected,
        validation_actual=wrapper.validation_actual,
        duration_s=duration_s,
        passed=error is None,
        error=str(error) if error is not None else None,
        group=wrapper.group,
        group_id=wrapper.group_id,
    )
    return result, error


def run_scenario(
    wrappers: list[Wrapper],
    scenario_path: Path,
    report_path: Path,
    session: LogSession | None = None,
    dut_logger: DutLogger | None = None,
    switchboard: Switchboard | None = None,
    scenario_name: str | None = None,
) -> None:
    """Execute a scenario's commands, then write its report.

    `scenario_name` is the scenario's own `name:` field, used to label the run
    in the report and in the combined log; without one both fall back to the
    scenario's filename.
    """
    started_at = datetime.now()
    wall_start = time.monotonic()
    results: list[TestResult] = []
    failure: Exception | None = None

    if session is not None:
        label = scenario_name or scenario_path.name
        session.write_marker(f"SCENARIO START: {label} ({len(wrappers)} commands)")

    try:
        for index, wrapper in enumerate(wrappers, start=1):
            _mark_command_start(session, wrapper, index, len(wrappers))
            result, failure = run_wrapper(wrapper)
            results.append(result)
            _mark_command_end(session, result, index, len(wrappers))
            if failure is not None:
                LOGGER.error("%s failed, stopping scenario: %s", type(wrapper).__name__, failure)
                break
            if wrapper.wait_after_s is not None:
                LOGGER.info("Waiting %s second(s) after %s", wrapper.wait_after_s, type(wrapper).__name__)
                time.sleep(wrapper.wait_after_s)
        else:
            LOGGER.info("Scenario execution finished")
    finally:
        # A command that raises part-way through - including a KeyboardInterrupt
        # during execute() or the wait_after_s sleep - must still leave the
        # broker connections closed, relays released, USB ports powered, and a
        # report written, or the client id stays taken by an orphan, relays stay
        # energized, the next run finds a dark DUT, and this run leaves no record.
        mqtt_registry.close_all()
        relay_cleanup_all()
        # Before the USB ports are restored: an unmount must happen while the
        # card's device still exists, and usb_switch_restore_all can power a
        # port back on but never puts back one this scenario cut.
        dut_storage_restore_all()
        usb_switch_restore_all(switchboard)
        _resume_dut_logging(dut_logger)

        total_duration_s = time.monotonic() - wall_start
        generate_report(
            scenario_path,
            started_at,
            total_duration_s,
            results,
            report_path,
            session,
            scenario_name,
        )
        LOGGER.info("Wrote test report to %s", report_path)

        if session is not None:
            passed = sum(1 for result in results if result.passed)
            session.write_marker(
                f"SCENARIO END: {passed}/{len(results)} passed in {total_duration_s:.2f}s"
            )

    if failure is not None:
        raise failure


def _resume_dut_logging(dut_logger: DutLogger | None) -> None:
    """Take the DUT console back if the scenario stopped while it was released.

    A scenario that fails between a `state: 0` and its matching `state: 1`
    would otherwise capture nothing for the rest of the run - including whatever
    the DUT says about the failure, which is the most useful part of the log at
    exactly that moment.

    Best effort by design: this runs in the runner's `finally`, so a console
    that cannot be recovered must be logged and not raised, or it would replace
    the failure that actually stopped the scenario.
    """
    if dut_logger is None or not dut_logger.is_paused:
        return
    try:
        dut_logger.resume()
    except (ConnectionError, OSError) as error:
        LOGGER.warning("Could not resume DUT console capture after the scenario ended: %s", error)


def _mark_command_start(session: LogSession | None, wrapper: Wrapper, index: int, total: int) -> None:
    """Write the combined log's start marker for one command.

    The command's `!Group` is appended when it has one, so a group is
    greppable in the logs and not only visible in the HTML report.
    """
    if session is None:
        return
    name = wrapper.name if getattr(wrapper, "name", None) else (wrapper.tag or type(wrapper).__name__)
    group = f" [{wrapper.group}]" if wrapper.group else ""
    session.write_marker(f"CMD {index}/{total} START: {name} (!{wrapper.tag}){group}")


def _mark_command_end(session: LogSession | None, result: TestResult, index: int, total: int) -> None:
    """Write the combined log's end marker, carrying the command's verdict."""
    if session is None:
        return
    verdict = "PASS" if result.passed else "FAIL"
    marker = f"CMD {index}/{total} END: {verdict} ({result.duration_s:.2f}s)"
    if not result.passed and result.error:
        marker = f"{marker}: {result.error}"
    session.write_marker(marker)


def start_dut_logging(session: LogSession, dut_log: DutLogConfig | None) -> DutLogger | None:
    """Begin DUT console capture, or explain in the log why there is none.

    Returns the running logger so the caller can stop it, or None when no
    console was configured. A console that is configured but cannot be opened
    raises: a run whose DUT was never attached would otherwise finish with a
    convincing but empty device log.
    """
    if dut_log is None:
        # Said out loud in the device log itself, so an empty one can't be
        # misread as "the DUT stayed quiet" when it actually means "no DUT
        # console was ever configured".
        session.write_device_note(
            "no DUT console configured for this run "
            "(pass --dut-log, or add a dut_log block to the scenario)"
        )
        return None

    dut_logger = DutLogger(session, port=dut_log.port, baud=dut_log.baud)
    dut_logger.start()
    # Marked on activation rather than on the first captured line, so a console
    # that was configured but stayed silent is still reported - an empty device
    # log is itself evidence when the DUT was supposed to be talking.
    session.mark_used(DEVICE_LOG)
    return dut_logger


def main() -> None:
    """Run one scenario: capture logs, execute its commands, write the report."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args = parse_args()

    # Opened before the scenario is parsed - the report path only depends on
    # the CLI args - so parse errors and per-command parse logs land in the
    # tool log too, which is where you look when a scenario won't load.
    report_path = resolve_report_path(args.test, args.report)
    if args.clean_results:
        # Before the session opens: LogSession.open() immediately creates
        # this run's own log files in the same directory, and cleaning after
        # that would unlink files this process still has open, silently
        # losing everything written to them.
        clean_results_dir(report_path.parent)
    session = LogSession(report_path)
    session.open()
    attach_log_handler(session)

    dut_logger: DutLogger | None = None
    dut_shell: DutShell | None = None
    try:
        LOGGER.info("Using test scenario file: %s", args.test)
        wrappers, scenario_name, scenario_dut_log, scenario_dut_cli, scenario_usb_hub = load_scenario(args.test)
        apply_cli_overrides(wrappers, args.port, args.firmware)

        # Both ports are resolved before anything is opened, so the checks that
        # need to compare them run while the bench is still untouched.
        dut_log = resolve_dut_log(scenario_dut_log, args.dut_log, args.dut_log_baud)
        dut_cli = resolve_dut_cli(scenario_dut_cli, args.dut_cli, args.dut_cli_baud)
        validate_dut_log_handover(wrappers, dut_log, dut_cli)

        dut_logger = start_dut_logging(session, dut_log)
        attach_dut_log_session(wrappers, session if dut_logger is not None else None)
        attach_dut_logger(wrappers, dut_logger)
        attach_mqtt_log_session(wrappers, session)
        dut_shell = attach_dut_cli_shell(wrappers, dut_cli, session)
        switchboard = attach_usb_switchboard(wrappers, scenario_usb_hub)
        run_scenario(
            wrappers, Path(args.test), report_path, session, dut_logger, switchboard, scenario_name
        )
    except Exception:
        # Logged rather than left to the default excepthook: that writes the
        # traceback straight to stderr, bypassing logging entirely, which would
        # leave the saved logs ending mid-stream with no reason why - exactly
        # the artefacts someone reads when a CI run fails and the terminal
        # output is long gone. Re-raised so the exit code is still non-zero.
        LOGGER.exception("Scenario run failed")
        raise
    finally:
        if dut_shell is not None:
            dut_shell.close()
        if dut_logger is not None:
            dut_logger.stop()
        written = ", ".join(str(path) for _, path in session.log_files())
        LOGGER.info("Wrote logs to %s", written)
        session.close()


if __name__ == "__main__":
    main()
