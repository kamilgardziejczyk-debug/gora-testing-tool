"""Background heartbeat: periodically re-sends every sensor's current state over UART."""

from __future__ import annotations

import threading
from typing import Callable, Optional

import serial

from registry import Registry
from sensors import Sensor


class Reporter:
    def __init__(self, ser: serial.Serial, registry: Registry, interval_s: float,
                 on_send: Optional[Callable[[Sensor], None]] = None):
        self._ser = ser
        self._registry = registry
        self._interval_s = interval_s
        self._on_send = on_send
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval_s + 1)

    def send_now(self, sensor: Sensor) -> None:
        self._ser.write(sensor.to_frame())

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            for sensor in self._registry.tick_all():
                self.send_now(sensor)
                if self._on_send:
                    self._on_send(sensor)
