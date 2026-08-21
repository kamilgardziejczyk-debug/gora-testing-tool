"""A live heart rate sensor simulation: peripheral + signal + streaming thread.

Owns the three pieces a caller would otherwise wire up itself (a
`BlePeripheral` serving the Heart Rate profile, an `HrvSignal`, and the
background thread that turns one into the other) and exposes one method per
scenario verb, so the REPL and the !BleHrvSim wrapper drive it identically.

The interesting part of testing a heart rate consumer is not the happy path -
it is a dropped beat, a stalled stream, a frame with more RR intervals than the
central budgeted for, and a sensor that vanishes mid-session. Each of those is
a method here rather than something a scenario has to improvise.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

from .hrv import DEFAULT_BPM, DEFAULT_JITTER_MS, HrvSignal
from .peripheral import DEFAULT_ADAPTER, BlePeripheral
from .profiles.heart_rate import (
    BATTERY_LEVEL_UUID,
    HEART_RATE_SERVICE_UUID,
    DEFAULT_BODY_SENSOR_LOCATION,
    HEART_RATE_CONTROL_POINT_UUID,
    HEART_RATE_MEASUREMENT_UUID,
    battery_service,
    device_information_service,
    encode_measurement,
    heart_rate_service,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 1.0
DEFAULT_BATTERY_PCT = 100

# How long `stop()` waits for the streaming thread to notice and finish its
# current notification. Generous relative to the cadence: a notification in
# flight is a blocking D-Bus round trip that should not be abandoned.
THREAD_JOIN_GRACE_S = 3.0


@dataclass(frozen=True)
class SimulatorStats:
    """What the simulator has actually put on the air so far.

    `rr_intervals` is the number a subscribed central was sent, which is what a
    scenario compares against rows recorded downstream - not the number of
    beats generated, since beats produced while nobody was subscribed are
    never sent and must not be counted.
    """

    notifications: int
    rr_intervals: int
    subscribed: bool
    bpm: int

    def describe(self) -> str:
        """One line summarising what has been sent and the current state."""
        state = "subscribed" if self.subscribed else "no subscriber"
        return (
            f"{self.notifications} notification(s), {self.rr_intervals} RR interval(s), "
            f"{self.bpm} bpm, {state}"
        )


class HeartRateSimulator:
    """A BLE heart rate sensor a DUT can find, connect to and stream from.

    Usage:

        simulator = HeartRateSimulator("GoraHRV_01", bpm=60)
        simulator.start()
        simulator.set_bpm(120)
        simulator.stop()

    Also usable as a context manager, which stops it on exit.

    Beats are only generated while a central is subscribed. A sensor whose
    notifications go nowhere is not interesting to simulate, and counting
    unsent beats would make the statistics a scenario asserts on meaningless.
    """

    def __init__(
        self,
        local_name: str,
        adapter: str = DEFAULT_ADAPTER,
        bpm: float = DEFAULT_BPM,
        jitter_ms: float = DEFAULT_JITTER_MS,
        drift_bpm_per_min: float = 0.0,
        interval_s: float = DEFAULT_INTERVAL_S,
        seed: Optional[int] = None,
        battery_pct: int = DEFAULT_BATTERY_PCT,
        location: str = DEFAULT_BODY_SENSOR_LOCATION,
        contact: Optional[bool] = True,
    ):
        if interval_s <= 0:
            raise ValueError(f"interval_s must be positive, got {interval_s}")

        self.local_name = local_name
        self.interval_s = interval_s
        self.signal = HrvSignal(bpm, jitter_ms, drift_bpm_per_min, seed)
        self.peripheral = BlePeripheral(
            local_name,
            [heart_rate_service(location), battery_service(battery_pct), device_information_service()],
            adapter=adapter,
            # Only the Heart Rate Service is advertised. Battery and Device
            # Information are still served, and a central reads them after
            # connecting - which is what a real sensor does, and it leaves the
            # advertising budget to the name a scenario has to match on.
            advertise=[HEART_RATE_SERVICE_UUID],
        )

        self._contact = contact
        self._energy: Optional[int] = None
        self._stalled_until = 0.0
        self._notifications = 0
        self._rr_sent = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "HeartRateSimulator":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    @property
    def is_running(self) -> bool:
        """Whether the sensor is advertising and its stream is live."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Advertise the sensor and begin streaming to whoever subscribes."""
        if self.is_running:
            raise RuntimeError(f"heart rate sensor '{self.local_name}' is already running")

        self.peripheral.start()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"hrv-{self.local_name}", daemon=True
        )
        self._thread.start()
        LOGGER.info(
            "Heart rate sensor '%s' streaming every %.2fs at %.0f bpm",
            self.local_name,
            self.interval_s,
            self.signal.bpm,
        )

    def stop(self) -> None:
        """Stop streaming and stop advertising. Idempotent, and restartable."""
        if self._thread is not None:
            self._stop_event.set()
            self._thread.join(timeout=self.interval_s + THREAD_JOIN_GRACE_S)
            if self._thread.is_alive():
                LOGGER.warning("Streaming thread for '%s' did not stop in time", self.local_name)
            self._thread = None

        self.peripheral.stop()
        LOGGER.info("Heart rate sensor '%s' stopped: %s", self.local_name, self.stats.describe())

    def close(self) -> None:
        """Stop, then shut the peripheral's event loop down. Idempotent."""
        self.stop()
        self.peripheral.close()

    @property
    def stats(self) -> SimulatorStats:
        """What has been sent so far, and the current pulse."""
        with self._lock:
            notifications, rr_intervals = self._notifications, self._rr_sent
        return SimulatorStats(
            notifications=notifications,
            rr_intervals=rr_intervals,
            subscribed=self.is_subscribed,
            bpm=round(self.signal.bpm),
        )

    @property
    def is_subscribed(self) -> bool:
        """Whether a central has subscribed to the measurement characteristic."""
        if not self.peripheral.is_started:
            return False
        return self.peripheral.is_notifying(HEART_RATE_MEASUREMENT_UUID)

    def set_bpm(self, bpm: float) -> None:
        """Change the pulse from the next beat on."""
        with self._lock:
            self.signal.set_bpm(bpm)

    def set_contact(self, contact: Optional[bool]) -> None:
        """Set skin contact: True, False, or None for a sensor not reporting it."""
        with self._lock:
            self._contact = contact
        LOGGER.info("Sensor contact for '%s' is now %s", self.local_name, contact)

    def set_energy(self, kilojoules: Optional[int]) -> None:
        """Report energy expended in every frame, or None to omit the field."""
        with self._lock:
            self._energy = kilojoules

    def set_battery(self, level_pct: int) -> None:
        """Update the battery level, notifying if the central subscribed to it."""
        if not 0 <= level_pct <= 100:
            raise ValueError(f"battery level must be between 0 and 100, got {level_pct}")
        self.peripheral.notify(BATTERY_LEVEL_UUID, bytes([level_pct]))
        LOGGER.info("Battery level for '%s' is now %d%%", self.local_name, level_pct)

    def stall(self, seconds: float) -> None:
        """Stay connected but send nothing for `seconds` - a stream gap.

        Returns straight away: the gap runs in the streaming thread, so the
        scenario can carry on and check what the DUT did about it.
        """
        if seconds < 0:
            raise ValueError(f"stall time cannot be negative, got {seconds}")
        with self._lock:
            self._stalled_until = time.monotonic() + seconds
        LOGGER.info("Sensor '%s' stalling for %.1fs", self.local_name, seconds)

    def resume(self) -> None:
        """End a stall early."""
        with self._lock:
            self._stalled_until = 0.0
        LOGGER.info("Sensor '%s' resumed", self.local_name)

    def send_burst(self, count: int) -> bool:
        """Send `count` RR intervals in one frame, whatever the cadence implies.

        The way to exceed what a central budgeted for: a frame carrying more
        intervals than it expects, or one longer than its receive buffer, is a
        real possibility on a congested link and is where parsers break.
        """
        if count < 1:
            raise ValueError(f"a burst needs at least one interval, got {count}")

        with self._lock:
            intervals: List[int] = []
            while len(intervals) < count:
                _, beats = self.signal.advance(self.interval_s)
                intervals.extend(beats)
            intervals = intervals[:count]
            frame = encode_measurement(round(self.signal.bpm), intervals, self._energy, self._contact)

        LOGGER.info("Sensor '%s' bursting %d RR interval(s), %d-byte frame",
                    self.local_name, count, len(frame))
        return self._send(frame, len(intervals))

    def bounce(self) -> None:
        """Drop off the air and come back, so the central must reconnect."""
        LOGGER.info("Sensor '%s' bouncing its connection", self.local_name)
        self.stop()
        self.start()

    def control_point_writes(self) -> List[bytes]:
        """Everything the central has written to the control point (0x2A39)."""
        return self.peripheral.writes(HEART_RATE_CONTROL_POINT_UUID)

    def _run(self) -> None:
        """Stream one measurement per interval until stopped."""
        while not self._stop_event.wait(self.interval_s):
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - a bad tick must not kill the stream
                LOGGER.exception("Error while streaming from '%s'", self.local_name)

    def _tick(self) -> None:
        """Generate and send one measurement, unless stalled or unsubscribed."""
        if not self.is_subscribed:
            return

        with self._lock:
            if time.monotonic() < self._stalled_until:
                return
            pulse, intervals = self.signal.advance(self.interval_s)
            frame = encode_measurement(pulse, intervals, self._energy, self._contact)

        self._send(frame, len(intervals))

    def _send(self, frame: bytes, interval_count: int) -> bool:
        """Notify the measurement characteristic and count what actually went out."""
        sent = self.peripheral.notify(HEART_RATE_MEASUREMENT_UUID, frame)
        if not sent:
            return False
        with self._lock:
            self._notifications += 1
            self._rr_sent += interval_count
        return True
