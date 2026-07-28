"""Interactive REPL for the BLE central.

Only tokenizes input and prints results - every operation goes through
`BleCentral`, the same API the !BleCentral wrapper uses.
"""

from __future__ import annotations

import argparse
import cmd

from .central import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_SCAN_TIMEOUT_S,
    BleCentral,
    DeviceNotFound,
)
from .values import DEFAULT_ENCODING, ENCODINGS, encode_value, format_value

EXIT_OK = 0
EXIT_CONNECTION_ERROR = 2


class BleShell(cmd.Cmd):
    intro = "BLE central. Type 'help' for commands, 'quit' to exit."
    prompt = "ble> "

    def __init__(self, central: BleCentral):
        super().__init__()
        self.central = central

    def do_scan(self, arg: str) -> None:
        """scan [seconds] - list advertising peripherals, strongest signal first."""
        timeout_s = self._parse_float(arg.strip()) if arg.strip() else None
        if arg.strip() and timeout_s is None:
            return

        devices = self.central.discover(timeout_s)
        if not devices:
            print("(no peripherals found)")
        for device in devices:
            print(device.describe())

    def do_connect(self, arg: str) -> None:
        """connect <name|address> - connect to a peripheral by advertised name or address."""
        target = arg.strip()
        if not target:
            print("usage: connect <name|address>")
            return

        try:
            device = self.central.connect(target)
        except DeviceNotFound as error:
            print(f"error: {error}")
            return
        except ConnectionError as error:
            print(f"error: {error}")
            return
        except RuntimeError as error:
            print(f"error: {error}")
            return
        print(f"connected to {device.describe()}")

    def do_disconnect(self, arg: str) -> None:
        """disconnect - drop the current connection."""
        self.central.disconnect()
        print("disconnected")

    def do_services(self, arg: str) -> None:
        """services - list the connected peripheral's services and characteristics."""
        try:
            services = self.central.services()
        except RuntimeError as error:
            print(f"error: {error}")
            return

        if not services:
            print("(no services)")
        for service in services:
            print(service.describe())

    def do_read(self, arg: str) -> None:
        """read <char_uuid> [service_uuid] - read a characteristic's value."""
        parts = arg.split()
        if not parts:
            print("usage: read <char_uuid> [service_uuid]")
            return

        char_uuid = parts[0]
        service_uuid = parts[1] if len(parts) > 1 else None
        try:
            data = self.central.read_characteristic(char_uuid, service_uuid)
        except (RuntimeError, ValueError, IOError) as error:
            print(f"error: {error}")
            return
        print(format_value(data))

    def do_write(self, arg: str) -> None:
        """write <char_uuid> <value> [encoding] [service_uuid] - write a characteristic.
        encoding is one of hex (default), utf8, uint8, uint16, uint32.
        Examples: 'write 2a00 01ff', 'write 2a00 hello utf8', 'write 2a00 42 uint16 180a'."""
        parts = arg.split()
        if len(parts) < 2:
            print("usage: write <char_uuid> <value> [encoding] [service_uuid]")
            return

        char_uuid, value = parts[0], parts[1]
        encoding = parts[2] if len(parts) > 2 else DEFAULT_ENCODING
        service_uuid = parts[3] if len(parts) > 3 else None

        try:
            data = encode_value(value, encoding)
            self.central.write_characteristic(char_uuid, data, service_uuid)
        except (RuntimeError, ValueError, IOError) as error:
            print(f"error: {error}")
            return
        print(f"wrote {format_value(data)}")

    def _parse_float(self, token: str):
        try:
            return float(token)
        except ValueError:
            print(f"invalid number '{token}'")
            return None

    def do_quit(self, arg: str) -> bool:
        """quit - disconnect and exit."""
        return True

    do_EOF = do_quit


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bluetooth LE central: scan, connect, read/write GATT")
    parser.add_argument("--adapter", default=None,
                        help="Bluetooth adapter to use, e.g. hci0 (default: system default)")
    parser.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT_S,
                        help=f"Seconds to scan for peripherals (default: {DEFAULT_SCAN_TIMEOUT_S})")
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT_S,
                        help=f"Seconds to wait for a connection (default: {DEFAULT_CONNECT_TIMEOUT_S})")
    parser.add_argument("--scan", action="store_true",
                        help="Scan once, print what was found, and exit without entering the REPL")
    parser.add_argument("--connect", default=None, metavar="NAME",
                        help="Connect to this peripheral (name or address) before the REPL starts")
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    central = BleCentral(
        adapter=args.adapter,
        scan_timeout_s=args.scan_timeout,
        connect_timeout_s=args.connect_timeout,
    )

    try:
        if args.scan:
            devices = central.discover()
            if not devices:
                print("(no peripherals found)")
            for device in devices:
                print(device.describe())
            return EXIT_OK

        if args.connect is not None:
            try:
                device = central.connect(args.connect)
            except ConnectionError as error:
                print(f"error: {error}")
                return EXIT_CONNECTION_ERROR
            print(f"connected to {device.describe()}")

        BleShell(central).cmdloop()
    except KeyboardInterrupt:
        print()
    finally:
        central.close()

    return EXIT_OK
