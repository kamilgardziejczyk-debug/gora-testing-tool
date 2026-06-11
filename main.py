import argparse

from parser import Parser


def main() -> None:
    argument_parser = argparse.ArgumentParser(description="Gora testing tool")
    argument_parser.add_argument(
        "-t",
        "--test",
        required=True,
        help="Path to YAML test scenario file",
    )
    args = argument_parser.parse_args()

    parser = Parser(args.test)

    if not parser.validate():
        raise ValueError("Passed test file is not a valid YAML file")

    parser.parse()


if __name__ == "__main__":
    main()
