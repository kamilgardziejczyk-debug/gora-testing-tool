"""Interactive REPLs for both BLE roles.

Only tokenizes input and prints results - every operation goes through
`BleCentral` or `HeartRateSimulator`, the same APIs the !BleCentral and
!BleHrvSim wrappers use.

Defaults to the central. `--peripheral NAME` starts the heart rate sensor
simulation instead, so a DUT's BLE side can be driven by hand before it is
written into a scenario.
"""

from __future__ import annotations

import argparse
import cmd

from .central import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_SCAN_TIMEOUT_S,
    BleCentral,
)
from .hr_simulator import DEFAULT_INTERVAL_S, HeartRateSimulator
from .hrv import DEFAULT_BPM, DEFAULT_JITTER_MS
from .peripheral import DEFAULT_ADAPTER, AdvertisingRejected
from .values import DEFAULT_ENCODING, ENCODINGS, encode_value, format_value

EXIT_OK = 0
EXIT_CONNECTION_ERROR = 2
EXIT_ADVERTISING_ERROR = 3


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


class HrvShell(cmd.Cmd):
    """REPL for the simulated heart rate sensor."""

    intro = "BLE heart rate sensor. Type 'help' for commands, 'quit' to exit."
    prompt = "hrv> "

    def __init__(self, simulator: HeartRateSimulator):
        super().__init__()
        self.simulator = simulator

    def do_status(self, arg: str) -> None:
        """status - what has been sent so far, and whether anyone is listening."""
        print(self.simulator.stats.describe())

    def do_gatt(self, arg: str) -> None:
        """gatt - the advertised name and the whole GATT table."""
        print(self.simulator.peripheral.describe())

    def do_bpm(self, arg: str) -> None:
        """bpm <value> - change the pulse, e.g. 'bpm 120'."""
        value = self._parse_float(arg.strip()) if arg.strip() else None
        if value is None:
            print("usage: bpm <value>")
            return
        try:
            self.simulator.set_bpm(value)
        except ValueError as error:
            print(f"error: {error}")
            return
        print(f"pulse is now {value:.0f} bpm")

    def do_contact(self, arg: str) -> None:
        """contact <yes|no|none> - skin contact state; 'none' stops reporting it."""
        states = {"yes": True, "no": False, "none": None}
        token = arg.strip().lower()
        if token not in states:
            print("usage: contact <yes|no|none>")
            return
        self.simulator.set_contact(states[token])
        print(f"contact is now {token}")

    def do_battery(self, arg: str) -> None:
        """battery <percent> - set and notify the battery level."""
        value = self._parse_float(arg.strip()) if arg.strip() else None
        if value is None:
            print("usage: battery <percent>")
            return
        try:
            self.simulator.set_battery(int(value))
        except (ValueError, RuntimeError) as error:
            print(f"error: {error}")
            return
        print(f"battery is now {int(value)}%")

    def do_energy(self, arg: str) -> None:
        """energy <kJ|off> - report energy expended in every frame, or stop."""
        token = arg.strip().lower()
        if not token:
            print("usage: energy <kJ|off>")
            return
        if token == "off":
            self.simulator.set_energy(None)
            print("energy expended is no longer reported")
            return

        value = self._parse_float(token)
        if value is None:
            return
        self.simulator.set_energy(int(value))
        print(f"energy expended is now {int(value)} kJ")

    def do_burst(self, arg: str) -> None:
        """burst <count> - send that many RR intervals in one frame.

        The way to exceed what a central budgeted for: 'burst 12' sends more
        intervals than a typical parser keeps room for."""
        value = self._parse_float(arg.strip()) if arg.strip() else None
        if value is None:
            print("usage: burst <count>")
            return
        try:
            sent = self.simulator.send_burst(int(value))
        except (ValueError, RuntimeError) as error:
            print(f"error: {error}")
            return
        print(f"burst of {int(value)} interval(s)" + ("" if sent else " (nobody subscribed)"))

    def do_stall(self, arg: str) -> None:
        """stall <seconds> - stay connected but send nothing for a while."""
        value = self._parse_float(arg.strip()) if arg.strip() else None
        if value is None:
            print("usage: stall <seconds>")
            return
        self.simulator.stall(value)
        print(f"stalling for {value:.1f}s")

    def do_resume(self, arg: str) -> None:
        """resume - end a stall early."""
        self.simulator.resume()
        print("resumed")

    def do_bounce(self, arg: str) -> None:
        """bounce - drop off the air and come back, forcing a reconnect."""
        try:
            self.simulator.bounce()
        except (RuntimeError, AdvertisingRejected) as error:
            print(f"error: {error}")
            return
        print("bounced")

    def do_writes(self, arg: str) -> None:
        """writes - everything the central wrote to the control point (0x2A39)."""
        values = self.simulator.control_point_writes()
        if not values:
            print("(no writes)")
        for value in values:
            print(format_value(value))

    def _parse_float(self, token: str):
        try:
            return float(token)
        except ValueError:
            print(f"invalid number '{token}'")
            return None

    def do_quit(self, arg: str) -> bool:
        """quit - stop the sensor and exit."""
        return True

    do_EOF = do_quit


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bluetooth LE: scan/connect/read/write as a central, or simulate a heart rate sensor"
    )
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

    peripheral = parser.add_argument_group(
        "peripheral role",
        "Simulate a BLE heart rate sensor for a DUT to connect to, instead of acting as a central",
    )
    peripheral.add_argument("--peripheral", default=None, metavar="NAME",
                            help="Advertise a heart rate sensor under this name and enter its REPL")
    peripheral.add_argument("--bpm", type=float, default=DEFAULT_BPM,
                            help=f"Starting pulse (default: {DEFAULT_BPM:.0f})")
    peripheral.add_argument("--jitter-ms", type=float, default=DEFAULT_JITTER_MS,
                            help=f"Beat-to-beat variability, the HRV itself (default: {DEFAULT_JITTER_MS:.0f})")
    peripheral.add_argument("--drift-bpm-per-min", type=float, default=0.0,
                            help="Pulse change per minute, e.g. 10 to ramp up (default: 0)")
    peripheral.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                            help=f"Seconds between notifications (default: {DEFAULT_INTERVAL_S})")
    peripheral.add_argument("--seed", type=int, default=None,
                            help="Seed the beat generator so a run is reproducible")
    return parser


def run_peripheral(args: argparse.Namespace) -> int:
    """Advertise a simulated heart rate sensor and drive it from a REPL."""
    simulator = HeartRateSimulator(
        args.peripheral,
        adapter=args.adapter or DEFAULT_ADAPTER,
        bpm=args.bpm,
        jitter_ms=args.jitter_ms,
        drift_bpm_per_min=args.drift_bpm_per_min,
        interval_s=args.interval,
        seed=args.seed,
    )

    try:
        simulator.start()
    except (AdvertisingRejected, ValueError) as error:
        print(f"error: {error}")
        simulator.close()
        return EXIT_ADVERTISING_ERROR

    print(simulator.peripheral.describe())
    try:
        HrvShell(simulator).cmdloop()
    except KeyboardInterrupt:
        print()
    finally:
        simulator.close()
    return EXIT_OK


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.peripheral is not None:
        return run_peripheral(args)

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
