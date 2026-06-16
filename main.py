import argparse
import logging

from parser import Parser


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
    args = argument_parser.parse_args()
    LOGGER.info("Using test scenario file: %s", args.test)

    parser = Parser(args.test)

    if not parser.validate():
        LOGGER.error("Invalid YAML file: %s", args.test)
        raise ValueError("Passed test file is not a valid YAML file")

    LOGGER.info("YAML validation successful")
    parser.parse()
    LOGGER.info("Scenario parsing finished")


if __name__ == "__main__":
    main()
