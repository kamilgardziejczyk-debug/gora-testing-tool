"""A live simulator session: serial link + sensor registry + heartbeat.

Owns the three pieces a caller would otherwise have to wire up itself (the
serial port, the `Registry` and the `Reporter`) and exposes one method per REPL
verb, so the command line front end and the `!SubghzSim` wrapper drive the
simulator through exactly the same API.

Every state change is sent on the wire immediately; the heartbeat keeps
re-sending each sensor's current state in the background until `close()`.
"""

from __future__ import annotations

import logging
from typing import List, Sequence, Tuple

import serial

from .registry import Registry
from .reporter import Reporter
from .sensors import Sensor, apply_fields

LOGGER = logging.getLogger(__name__)

DEFAULT_BAUD = 115200
DEFAULT_INTERVAL_S = 5.0
SERIAL_READ_TIMEOUT_S = 1


class SubghzSimulator:
    """A serial link that reports simulated sub-GHz sensors to a gateway.

    Usage:

        simulator = SubghzSimulator("/dev/ttyUSB0", 115200)
        simulator.open()
        sensor = simulator.add_sensor("temp_hum")
        simulator.update_sensor(sensor.sensor_id, [("temp", "22.5")])
        simulator.close()

    Also usable as a context manager, which closes the link on exit.

    Sensor ids are assigned per session, starting at 1: a new
    `SubghzSimulator` always numbers its first sensor #1.
    """

    def __init__(
        self,
        port: str,
        baud: int = DEFAULT_BAUD,
        interval_s: float = DEFAULT_INTERVAL_S,
    ):
        self.port = port
        self.baud = baud
        self.interval_s = interval_s

        self._registry = Registry()
        self._serial: serial.Serial | None = None
        self._reporter: Reporter | None = None
        self._closed = False

    def __enter__(self) -> "SubghzSimulator":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def open(self) -> None:
        """Open the serial port and start the background heartbeat."""
        try:
            self._serial = serial.Serial(self.port, self.baud, timeout=SERIAL_READ_TIMEOUT_S)
        except (serial.SerialException, ValueError) as error:
            raise ConnectionError(
                f"could not open serial port {self.port} at {self.baud} baud ({error})"
            ) from error

        self._reporter = Reporter(self._serial, self._registry, self.interval_s)
        self._reporter.start()
        LOGGER.info(
            "Simulator active on %s at %d baud, heartbeat every %.1fs",
            self.port,
            self.baud,
            self.interval_s,
        )

    def close(self) -> None:
        """Stop the heartbeat and close the serial port. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._reporter is not None:
            self._reporter.stop()
        if self._serial is not None:
            self._serial.close()
        LOGGER.info("Simulator on %s closed", self.port)

    def add_sensor(self, type_name: str) -> Sensor:
        """Add a sensor of `type_name` and send its first frame.

        Raises `UnknownSensorType` if the type is not one of `SENSOR_TYPES`.
        """
        # Checked before the registry is touched: a sensor that could not be
        # announced must not be left behind for the heartbeat to report.
        self._ensure_open()
        sensor = self._registry.add(type_name)
        self._send(sensor)
        LOGGER.info("Added sensor %s", sensor.describe())
        return sensor

    def remove_sensor(self, sensor_id: int) -> None:
        """Stop reporting a sensor. Raises `UnknownSensorId` if it is not there."""
        self._ensure_open()
        self._registry.remove(sensor_id)
        LOGGER.info("Removed sensor #%d", sensor_id)

    def get_sensor(self, sensor_id: int) -> Sensor:
        """Look up a live sensor. Raises `UnknownSensorId` if it is not there."""
        return self._registry.get(sensor_id)

    def list_sensors(self) -> List[Sensor]:
        """Every live sensor, ordered by id."""
        return self._registry.list()

    def update_sensor(self, sensor_id: int, fields: Sequence[Tuple[str, str]]) -> Sensor:
        """Apply `(field, value)` pairs to a sensor and send the new state at once.

        Raises `UnknownSensorId` for an unknown sensor, or `ValueError` for a
        field the sensor's type does not have. Validation happens before
        anything is mutated, so a rejected update leaves the sensor untouched.
        """
        self._ensure_open()
        sensor = self._registry.get(sensor_id)
        apply_fields(sensor, fields)
        self._send(sensor)
        LOGGER.info("Updated sensor %s", sensor.describe())
        return sensor

    def _send(self, sensor: Sensor) -> None:
        """Send one sensor's current state on the wire."""
        self._ensure_open()
        self._reporter.send_now(sensor)

    def _ensure_open(self) -> None:
        """Reject any command issued before `open()` or after `close()`."""
        if self._reporter is None or self._closed:
            raise RuntimeError(f"simulator on {self.port} is not open: call open() first")
