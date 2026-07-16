"""Interactive REPL for the sub-GHz sensor simulator."""

from __future__ import annotations

import argparse
import cmd

import serial

from registry import Registry, UnknownSensorId, UnknownSensorType
from reporter import Reporter
from sensors import SENSOR_TYPES, Sensor, TempHum


class SubghzShell(cmd.Cmd):
    intro = "Sub-GHz sensor simulator. Type 'help' for commands, 'quit' to exit."
    prompt = "subghz> "

    def __init__(self, registry: Registry, reporter: Reporter):
        super().__init__()
        self.registry = registry
        self.reporter = reporter

    def do_add(self, arg: str) -> None:
        """add <heat|smoke|co|temp_hum> - add a new sensor and start reporting it."""
        type_name = arg.strip().lower()
        if not type_name:
            print("usage: add <heat|smoke|co|temp_hum>")
            return
        try:
            sensor = self.registry.add(type_name)
        except UnknownSensorType:
            choices = ", ".join(sorted(set(SENSOR_TYPES)))
            print(f"unknown sensor type '{type_name}' (choices: {choices})")
            return
        self.reporter.send_now(sensor)
        print(f"added {sensor.describe()}")

    def do_del(self, arg: str) -> None:
        """del <sensor_id> - remove a sensor."""
        sensor_id = self._parse_id(arg.strip())
        if sensor_id is None:
            return
        try:
            self.registry.remove(sensor_id)
        except UnknownSensorId:
            print(f"no sensor #{sensor_id}")
            return
        print(f"removed #{sensor_id}")

    def do_list(self, arg: str) -> None:
        """list - show all sensors and their current state."""
        sensors = self.registry.list()
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
            sensor = self.registry.get(sensor_id)
        except UnknownSensorId:
            print(f"no sensor #{sensor_id}")
            return

        try:
            self._apply_set(sensor, parts[1:])
        except ValueError as exc:
            print(f"error: {exc}")
            return

        self.reporter.send_now(sensor)
        print(sensor.describe())

    def _apply_set(self, sensor: Sensor, tokens: list) -> None:
        if len(tokens) % 2 != 0:
            raise ValueError("fields must be given as '<field> <value>' pairs")

        pairs = list(zip(tokens[0::2], tokens[1::2]))

        if isinstance(sensor, TempHum):
            temp_c = None
            humidity_pct = None
            for field, value in pairs:
                if field == "status":
                    if value not in ("online", "offline"):
                        raise ValueError(f"unrecognized status '{value}'")
                    sensor.online = value == "online"
                elif field == "temp":
                    temp_c = float(value)
                elif field == "humidity":
                    humidity_pct = float(value)
                else:
                    raise ValueError(f"unrecognized field '{field}'")
            if temp_c is not None or humidity_pct is not None:
                sensor.set_values(temp_c=temp_c, humidity_pct=humidity_pct)
        else:
            for field, value in pairs:
                if field == "status":
                    if value not in ("online", "offline"):
                        raise ValueError(f"unrecognized status '{value}'")
                    sensor.online = value == "online"
                elif field == "alarm":
                    if value not in ("on", "off"):
                        raise ValueError(f"unrecognized alarm value '{value}'")
                    sensor.alarm = value == "on"
                else:
                    raise ValueError(f"unrecognized field '{field}'")

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
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument("--interval", type=float, default=5.0,
                         help="Heartbeat interval in seconds (default: 5)")
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    registry = Registry()
    with serial.Serial(args.port, args.baud, timeout=1) as ser:
        reporter = Reporter(ser, registry, args.interval)
        reporter.start()
        try:
            SubghzShell(registry, reporter).cmdloop()
        finally:
            reporter.stop()

    return 0
