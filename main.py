import argparse
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from parser import DutLogConfig, Parser
from reporting import TestResult, generate_report
from tools.dut_logger import DEFAULT_BAUD as DEFAULT_DUT_BAUD
from tools.dut_logger import DutLogger, LogSession, attach as attach_log_handler
from wrappers import Wrapper, mqtt_registry, relay_cleanup_all


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
    return argument_parser.parse_args()


def load_scenario(test_file: str) -> tuple[list[Wrapper], DutLogConfig | None]:
    """Validate and parse a scenario into its commands and DUT log settings.

    Both come from one Parser so the file is read (and composed) only once.
    """
    parser = Parser(test_file)
    if not parser.validate():
        LOGGER.error("Invalid YAML file: %s", test_file)
        raise ValueError("Passed test file is not a valid YAML file")
    LOGGER.info("YAML validation successful")
    dut_log_config = parser.parse_dut_log()
    wrappers = parser.parse()
    LOGGER.info("Scenario parsing finished, executing %d commands", len(wrappers))
    return wrappers, dut_log_config


def resolve_dut_log(
    scenario_config: DutLogConfig | None,
    port_arg: str | None,
    baud_arg: int | None,
) -> DutLogConfig | None:
    """Merge the scenario's `dut_log` block with the CLI overrides.

    `--dut-log` wins over the scenario's port, matching how `--port` and
    `--firmware` already override their YAML equivalents. `--dut-log-baud`
    can be given on its own to re-rate a port the scenario declared.
    """
    port = port_arg or (scenario_config.port if scenario_config else None)
    if port is None:
        return None

    baud = baud_arg or (scenario_config.baud if scenario_config else None) or DEFAULT_DUT_BAUD
    if port_arg is not None and scenario_config is not None and port_arg != scenario_config.port:
        LOGGER.info("Overriding scenario DUT log port %s with CLI value: %s", scenario_config.port, port_arg)
    return DutLogConfig(port=port, baud=baud)


def apply_cli_overrides(wrappers: list[Wrapper], port: str | None, firmware: str | None) -> None:
    for wrapper in wrappers:
        if port is not None and wrapper.supports_port_override:
            LOGGER.info("Overriding %s serial port with CLI value: %s", wrapper.tag, port)
            wrapper.port = port
        if firmware is not None and wrapper.supports_firmware_dir_override:
            LOGGER.info("Overriding %s firmware directory with CLI value: %s", wrapper.tag, firmware)
            wrapper.firmware_dir = firmware


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
    )
    return result, error


def run_scenario(
    wrappers: list[Wrapper],
    scenario_path: Path,
    report_path: Path,
    session: LogSession | None = None,
) -> None:
    started_at = datetime.now()
    wall_start = time.monotonic()
    results: list[TestResult] = []
    failure: Exception | None = None

    if session is not None:
        session.write_marker(f"SCENARIO START: {scenario_path.name} ({len(wrappers)} commands)")

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
        # broker connections closed, relays released, and a report written,
        # or the client id stays taken by an orphan, relays stay energized, and
        # the run leaves no record.
        mqtt_registry.close_all()
        relay_cleanup_all()

        total_duration_s = time.monotonic() - wall_start
        generate_report(scenario_path, started_at, total_duration_s, results, report_path, session)
        LOGGER.info("Wrote test report to %s", report_path)

        if session is not None:
            passed = sum(1 for result in results if result.passed)
            session.write_marker(
                f"SCENARIO END: {passed}/{len(results)} passed in {total_duration_s:.2f}s"
            )

    if failure is not None:
        raise failure


def _mark_command_start(session: LogSession | None, wrapper: Wrapper, index: int, total: int) -> None:
    """Write the combined log's start marker for one command."""
    if session is None:
        return
    name = wrapper.name if getattr(wrapper, "name", None) else (wrapper.tag or type(wrapper).__name__)
    session.write_marker(f"CMD {index}/{total} START: {name} (!{wrapper.tag})")


def _mark_command_end(session: LogSession | None, result: TestResult, index: int, total: int) -> None:
    """Write the combined log's end marker, carrying the command's verdict."""
    if session is None:
        return
    verdict = "PASS" if result.passed else "FAIL"
    marker = f"CMD {index}/{total} END: {verdict} ({result.duration_s:.2f}s)"
    if not result.passed and result.error:
        marker = f"{marker}: {result.error}"
    session.write_marker(marker)


def start_dut_logging(
    session: LogSession,
    scenario_dut_log: DutLogConfig | None,
    args: argparse.Namespace,
) -> DutLogger | None:
    """Begin DUT console capture, or explain in the log why there is none.

    Returns the running logger so the caller can stop it, or None when no
    console was configured. A console that is configured but cannot be opened
    raises: a run whose DUT was never attached would otherwise finish with a
    convincing but empty device log.
    """
    dut_log = resolve_dut_log(scenario_dut_log, args.dut_log, args.dut_log_baud)
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
    session = LogSession(report_path)
    session.open()
    attach_log_handler(session)

    dut_logger: DutLogger | None = None
    try:
        LOGGER.info("Using test scenario file: %s", args.test)
        wrappers, scenario_dut_log = load_scenario(args.test)
        apply_cli_overrides(wrappers, args.port, args.firmware)
        dut_logger = start_dut_logging(session, scenario_dut_log, args)
        run_scenario(wrappers, Path(args.test), report_path, session)
    except Exception:
        # Logged rather than left to the default excepthook: that writes the
        # traceback straight to stderr, bypassing logging entirely, which would
        # leave the saved logs ending mid-stream with no reason why - exactly
        # the artefacts someone reads when a CI run fails and the terminal
        # output is long gone. Re-raised so the exit code is still non-zero.
        LOGGER.exception("Scenario run failed")
        raise
    finally:
        if dut_logger is not None:
            dut_logger.stop()
        LOGGER.info("Wrote logs to %s, %s, %s", session.tool_path, session.device_path, session.combined_path)
        session.close()


if __name__ == "__main__":
    main()
