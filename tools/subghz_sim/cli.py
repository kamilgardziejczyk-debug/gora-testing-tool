"""Interactive REPL for the sub-GHz sensor simulator.

Only tokenizes input and prints results — every state change goes through
`SubghzSimulator`, the same API the !SubghzSim wrapper uses.
"""

from __future__ import annotations

import argparse
import cmd

from .registry import UnknownSensorId, UnknownSensorType
from .sensors import SENSOR_TYPES, parse_field_pairs
from .simulator import DEFAULT_BAUD, DEFAULT_INTERVAL_S, SubghzSimulator

EXIT_OK = 0
EXIT_CONNECTION_ERROR = 2


class SubghzShell(cmd.Cmd):
    intro = "Sub-GHz sensor simulator. Type 'help' for commands, 'quit' to exit."
    prompt = "subghz> "

    def __init__(self, simulator: SubghzSimulator):
        super().__init__()
        self.simulator = simulator

    def do_add(self, arg: str) -> None:
        """add <heat|smoke|co|temp_hum> - add a new sensor and start reporting it."""
        type_name = arg.strip().lower()
        if not type_name:
            print("usage: add <heat|smoke|co|temp_hum>")
            return
        try:
            sensor = self.simulator.add_sensor(type_name)
        except UnknownSensorType:
            choices = ", ".join(sorted(set(SENSOR_TYPES)))
            print(f"unknown sensor type '{type_name}' (choices: {choices})")
            return
        print(f"added {sensor.describe()}")

    def do_del(self, arg: str) -> None:
        """del <sensor_id> - remove a sensor."""
        sensor_id = self._parse_id(arg.strip())
        if sensor_id is None:
            return
        try:
            self.simulator.remove_sensor(sensor_id)
        except UnknownSensorId:
            print(f"no sensor #{sensor_id}")
            return
        print(f"removed #{sensor_id}")

    def do_list(self, arg: str) -> None:
        """list - show all sensors and their current state."""
        sensors = self.simulator.list_sensors()
        if not sensors:
            print("(no sensors)")
        for sensor in sensors:
            print(sensor.describe())

    def do_set(self, arg: str) -> None:
        """set <sensor_id> <field> <value> [<field> <value> ...] - update a sensor's
        state and send it immediately.
        Alarm sensors: 'set 1 status online', 'set 1 status offline',
                       'set 1 alarm on', 'set 1 alarm off'.
        temp_hum: 'set 2 status online', 'set 2 temp 22.5', 'set 2 humidity 48'
                  (also combinable: 'set 2 temp 22.5 humidity 48');
                  setting temp/humidity pins the value until changed again."""
        parts = arg.split()
        if len(parts) < 3:
            print("usage: set <sensor_id> <field> <value> [<field> <value> ...]")
            return

        sensor_id = self._parse_id(parts[0])
        if sensor_id is None:
            return

        try:
            sensor = self.simulator.update_sensor(sensor_id, parse_field_pairs(parts[1:]))
        except UnknownSensorId:
            print(f"no sensor #{sensor_id}")
            return
        except ValueError as exc:
            print(f"error: {exc}")
            return

        print(sensor.describe())

    def _parse_id(self, token: str):
        try:
            return int(token)
        except ValueError:
            print(f"invalid sensor id '{token}'")
            return None

    def do_quit(self, arg: str) -> bool:
        """quit - exit the simulator."""
        return True

    do_EOF = do_quit


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sub-GHz sensor simulator")
    parser.add_argument("--port", required=True, help="Serial port, e.g. /dev/ttyUSB0 or COM5")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                         help=f"Baud rate (default: {DEFAULT_BAUD})")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                         help=f"Heartbeat interval in seconds (default: {DEFAULT_INTERVAL_S})")
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    simulator = SubghzSimulator(port=args.port, baud=args.baud, interval_s=args.interval)
    try:
        simulator.open()
    except ConnectionError as error:
        print(f"error: {error}")
        return EXIT_CONNECTION_ERROR

    try:
        SubghzShell(simulator).cmdloop()
    except KeyboardInterrupt:
        print()
    finally:
        simulator.close()

    return EXIT_OK
