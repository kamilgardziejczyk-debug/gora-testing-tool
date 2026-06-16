import argparse
import logging

from parser import Parser
from wrappers import ProgramEsptoolWarpper


LOGGER = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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
    args = argument_parser.parse_args()
    LOGGER.info("Using test scenario file: %s", args.test)

    parser = Parser(args.test)

    if not parser.validate():
        LOGGER.error("Invalid YAML file: %s", args.test)
        raise ValueError("Passed test file is not a valid YAML file")

    LOGGER.info("YAML validation successful")
    wrappers = parser.parse()
    LOGGER.info("Scenario parsing finished, executing %d commands", len(wrappers))

    if args.port is not None:
        for wrapper in wrappers:
            if isinstance(wrapper, ProgramEsptoolWarpper):
                LOGGER.info("Overriding serial port with CLI value: %s", args.port)
                wrapper.port = args.port

    for wrapper in wrappers:
        wrapper.execute()

    LOGGER.info("Scenario execution finished")


if __name__ == "__main__":
    main()
