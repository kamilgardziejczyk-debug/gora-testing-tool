import argparse
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from parser import Parser
from reporting import TestResult, generate_report
from wrappers import ProgramEsptoolWarpper, ProgramJlinkWarpper, Wrapper, mqtt_registry


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
    return argument_parser.parse_args()


def load_scenario(test_file: str) -> list[Wrapper]:
    parser = Parser(test_file)
    if not parser.validate():
        LOGGER.error("Invalid YAML file: %s", test_file)
        raise ValueError("Passed test file is not a valid YAML file")
    LOGGER.info("YAML validation successful")
    wrappers = parser.parse()
    LOGGER.info("Scenario parsing finished, executing %d commands", len(wrappers))
    return wrappers


def apply_cli_overrides(wrappers: list[Wrapper], port: str | None, firmware: str | None) -> None:
    for wrapper in wrappers:
        if isinstance(wrapper, ProgramEsptoolWarpper):
            if port is not None:
                LOGGER.info("Overriding serial port with CLI value: %s", port)
                wrapper.port = port
            if firmware is not None:
                LOGGER.info("Overriding firmware directory with CLI value: %s", firmware)
                wrapper.firmware_dir = firmware
        elif isinstance(wrapper, ProgramJlinkWarpper):
            if firmware is not None:
                LOGGER.info("Overriding J-Link firmware directory with CLI value: %s", firmware)
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


def run_scenario(wrappers: list[Wrapper], scenario_path: Path, report_path: Path) -> None:
    started_at = datetime.now()
    wall_start = time.monotonic()
    results: list[TestResult] = []
    failure: Exception | None = None

    for wrapper in wrappers:
        result, failure = run_wrapper(wrapper)
        results.append(result)
        if failure is not None:
            LOGGER.error("%s failed, stopping scenario: %s", type(wrapper).__name__, failure)
            break
        if wrapper.wait_after_s is not None:
            LOGGER.info("Waiting %s second(s) after %s", wrapper.wait_after_s, type(wrapper).__name__)
            time.sleep(wrapper.wait_after_s)
    else:
        LOGGER.info("Scenario execution finished")

    # A command that raises part-way through must still leave the broker
    # connections closed, or the client id stays taken by an orphan.
    mqtt_registry.close_all()

    total_duration_s = time.monotonic() - wall_start
    generate_report(scenario_path, started_at, total_duration_s, results, report_path)
    LOGGER.info("Wrote test report to %s", report_path)

    if failure is not None:
        raise failure


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args = parse_args()
    LOGGER.info("Using test scenario file: %s", args.test)

    wrappers = load_scenario(args.test)
    apply_cli_overrides(wrappers, args.port, args.firmware)
    report_path = resolve_report_path(args.test, args.report)
    run_scenario(wrappers, Path(args.test), report_path)


if __name__ == "__main__":
    main()
