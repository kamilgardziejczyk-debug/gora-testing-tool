import argparse
import logging

from parser import Parser
from wrappers import ProgramEsptoolWarpper, Wrapper


LOGGER = logging.getLogger(__name__)


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
        help="Path to directory containing firmware binaries (bootloader, partition table, firmware). Overrides the directory for all ProgramEsptool commands.",
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


def run_scenario(wrappers: list[Wrapper]) -> None:
    for wrapper in wrappers:
        wrapper.execute()
    LOGGER.info("Scenario execution finished")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args = parse_args()
    LOGGER.info("Using test scenario file: %s", args.test)

    wrappers = load_scenario(args.test)
    apply_cli_overrides(wrappers, args.port, args.firmware)
    run_scenario(wrappers)


if __name__ == "__main__":
    main()
