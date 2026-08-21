"""!BleHrvSimStart - begin advertising a simulated BLE heart rate sensor."""

import logging

import yaml

from tools.ble_gatt.hr_simulator import (
    DEFAULT_BATTERY_PCT,
    DEFAULT_INTERVAL_S,
    HeartRateSimulator,
)
from tools.ble_gatt.hrv import DEFAULT_BPM, DEFAULT_JITTER_MS
from tools.ble_gatt.peripheral import DEFAULT_ADAPTER
from tools.ble_gatt.profiles.heart_rate import (
    BODY_SENSOR_LOCATIONS,
    DEFAULT_BODY_SENSOR_LOCATION,
)

from . import ble_hrv_registry
from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

CONTACT_STATES = {"yes": True, "no": False, "none": None}


class BleHrvSimStartWrapper(Wrapper):
    """Starts a simulated heart rate sensor and leaves it running.

    Unlike !BleCentral, which owns its connection for one command, this sensor
    has to stay on the air while later commands run - a DUT records from it
    across a whole session. It is therefore registered under `session`, driven
    by !BleHrvSimSet, and ended by !BleHrvSimStop. The runner stops any session
    still running when the scenario ends, so a scenario that fails part-way
    still frees the adapter.

    Nothing is streamed until a central subscribes: beats produced with nobody
    listening would be counted but never sent, which would make the totals
    !BleHrvSimStop asserts on meaningless. A scenario waits for that moment on
    the DUT's own console - the first measurement it logs is proof it
    subscribed, and proof it parsed what arrived, which the sensor's own view
    of its subscription is not.
    """

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.session: str | None = None
        self.device: str | None = None
        self.adapter: str = DEFAULT_ADAPTER
        self.bpm: float = DEFAULT_BPM
        self.jitter_ms: float = DEFAULT_JITTER_MS
        self.drift_bpm_per_min: float = 0.0
        self.interval_s: float = DEFAULT_INTERVAL_S
        self.seed: int | None = None
        self.battery_pct: int = DEFAULT_BATTERY_PCT
        self.location: str = DEFAULT_BODY_SENSOR_LOCATION
        self.contact: bool | None = True

    def parse(self) -> None:
        """Read the sensor's configuration, rejecting anything invalid up front."""
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "BleHrvSimStart":
            raise ValueError("Expected !BleHrvSimStart command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue
            self._apply_field(key_node.value, value_node.value)

        if self.session is None:
            raise ValueError("BleHrvSimStart: 'session' field is required")
        if self.device is None:
            raise ValueError(
                "BleHrvSimStart: 'device' field is required - it is the name the sensor "
                "advertises, and the DUT must be configured to look for exactly this name"
            )
        self._validate()

        LOGGER.info(
            "Parsed BleHrvSimStart: name=%s, session=%s, device=%s, adapter=%s, bpm=%s, "
            "jitter_ms=%s, interval_s=%s, seed=%s",
            self.name, self.session, self.device, self.adapter, self.bpm,
            self.jitter_ms, self.interval_s, self.seed,
        )

    def _apply_field(self, key: str, value: str) -> None:
        """Read one YAML field into its attribute, converting as it goes."""
        if key == "name":
            self.name = value
        elif key == "session":
            self.session = value
        elif key == "device":
            self.device = value
        elif key == "adapter":
            self.adapter = value
        elif key == "bpm":
            self.bpm = float(value)
        elif key == "jitter_ms":
            self.jitter_ms = float(value)
        elif key == "drift_bpm_per_min":
            self.drift_bpm_per_min = float(value)
        elif key == "interval_s":
            self.interval_s = float(value)
        elif key == "seed":
            self.seed = int(value)
        elif key == "battery_pct":
            self.battery_pct = int(value)
        elif key == "location":
            self.location = value
        elif key == "contact":
            self.contact = self._parse_contact(value)

    def _validate(self) -> None:
        """Reject anything decidable from the YAML before the radio is touched."""
        if self.location not in BODY_SENSOR_LOCATIONS:
            raise ValueError(
                f"BleHrvSimStart: unknown location '{self.location}' "
                f"(choices: {', '.join(sorted(BODY_SENSOR_LOCATIONS))})"
            )
        if self.interval_s <= 0:
            raise ValueError(f"BleHrvSimStart: 'interval_s' must be positive, got {self.interval_s}")
        if not 0 <= self.battery_pct <= 100:
            raise ValueError(
                f"BleHrvSimStart: 'battery_pct' must be between 0 and 100, got {self.battery_pct}"
            )

    def _parse_contact(self, value: str) -> bool | None:
        """Read the three-state contact field, which is not a plain boolean."""
        token = value.strip().lower()
        if token not in CONTACT_STATES:
            raise ValueError(
                f"BleHrvSimStart: 'contact' must be one of "
                f"{', '.join(sorted(CONTACT_STATES))}, got '{value}'"
            )
        return CONTACT_STATES[token]

    def execute(self) -> None:
        """Build the sensor, put it on the air, and register it under `session`."""
        simulator = HeartRateSimulator(
            self.device,
            adapter=self.adapter,
            bpm=self.bpm,
            jitter_ms=self.jitter_ms,
            drift_bpm_per_min=self.drift_bpm_per_min,
            interval_s=self.interval_s,
            seed=self.seed,
            battery_pct=self.battery_pct,
            location=self.location,
            contact=self.contact,
        )

        simulator.start()
        try:
            ble_hrv_registry.register(self.session, simulator)
        except ValueError:
            # The name clash is the scenario's error, but the sensor is already
            # on the air - leaving it there would hold the adapter for the rest
            # of the run with nothing able to reach it.
            simulator.close()
            raise

        LOGGER.info(
            "BLE HRV sensor '%s' advertising as session '%s' at %.0f bpm",
            self.device, self.session, self.bpm,
        )
