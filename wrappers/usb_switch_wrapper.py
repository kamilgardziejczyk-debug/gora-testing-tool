import logging

import yaml

from tools.usb_hub import PORT_COUNT, Switchboard

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

# Ports this scenario has powered off, so the runner's cleanup can put them
# back. Module-level for the same reason relay_control_wrapper's board is: the
# Parser builds a fresh wrapper per command and has nowhere to hang state that
# outlives one of them, and the thing being tracked belongs to the whole run.
_powered_off: set[int] = set()


class UsbSwitchWrapper(Wrapper):
    """Wrapper for switching power on one port of the MEGA4 USB hub.

    Cutting a port drops VBUS to whatever is plugged into it, so this is a
    hard power cycle of a DUT rather than a soft reset - which is the point,
    and also why it is worth knowing that powering *off* takes a few seconds
    (see tools/usb_hub) while powering on is immediate.
    """

    requires_usb_hub = True

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.usb_port: str | None = None
        self.state: bool | None = None
        self.cycle_s: float | None = None

    def parse(self) -> None:
        """Read the command's YAML fields and reject an unusable combination."""
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "UsbSwitch":
            raise ValueError("Expected !UsbSwitch command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "port":
                self.usb_port = value_node.value
            elif key == "state":
                self.state = self._parse_state(value_node.value)
            elif key == "cycle_s":
                self.cycle_s = float(value_node.value)

        self._validate_parsed_fields()

        LOGGER.info(
            "Parsed UsbSwitch: name=%s, port=%s, state=%s, cycle_s=%s",
            self.name,
            self.usb_port,
            self.state,
            self.cycle_s,
        )

    @staticmethod
    def _parse_state(raw: str) -> bool:
        """Parse the 'state' field: 1/true powered, 0/false unpowered.

        Both spellings are accepted because the two neighbouring tags disagree -
        !RelayControl uses 1/0 and this tag was documented as true/false - and a
        scenario author should not have to remember which is which.
        """
        value = raw.strip().lower()
        if value in {"1", "true"}:
            return True
        if value in {"0", "false"}:
            return False
        raise ValueError(f"UsbSwitch: 'state' must be 1/0 or true/false, got '{raw}'")

    def _validate_parsed_fields(self) -> None:
        """Reject a scenario missing/conflicting YAML fields, before any hardware is touched."""
        if self.usb_port is None:
            raise ValueError(
                f"UsbSwitch: 'port' field is required - a port number 1..{PORT_COUNT}, or a "
                f"name from the scenario's usb_hub.ports block"
            )
        if self.state is not None and self.cycle_s is not None:
            raise ValueError("UsbSwitch: specify either 'state' or 'cycle_s', not both")
        if self.state is None and self.cycle_s is None:
            raise ValueError("UsbSwitch: one of 'state' or 'cycle_s' is required")
        if self.cycle_s is not None and self.cycle_s < 0:
            raise ValueError(f"UsbSwitch: 'cycle_s' must be >= 0, got {self.cycle_s}")

    def execute(self) -> None:
        """Apply the parsed state or power cycle to the command's port."""
        switchboard = self._require_switchboard()
        target = self.usb_port

        if self.cycle_s is not None:
            switchboard.cycle(target, self.cycle_s)
            _powered_off.discard(switchboard.resolve(target))
            return

        switchboard.set(target, self.state)
        _track(switchboard.resolve(target), self.state)

    def _require_switchboard(self) -> Switchboard:
        """The run's switchboard, or the misconfiguration that explains its absence."""
        if self.usb_switchboard is None:
            raise RuntimeError(
                "UsbSwitch: no USB hub is attached to this run. This is a wiring mistake in "
                "the runner, not in the scenario - the switchboard is built for every "
                "scenario containing a !UsbSwitch command."
            )
        return self.usb_switchboard


def _track(port: int, powered: bool) -> None:
    """Remember whether `port` still needs restoring when the scenario ends."""
    if powered:
        _powered_off.discard(port)
    else:
        _powered_off.add(port)


def restore_all(switchboard: Switchboard | None) -> None:
    """Power any port this scenario switched off back on.

    Called from the scenario runner's `finally`, mirroring relay cleanup: a run
    that fails between a `state: 0` and its matching `state: 1` would otherwise
    leave the DUT dark, and the *next* run would fail too - for a reason that
    has nothing to do with what it was testing. A scenario deliberately ending
    with a port off gets it powered back on, which is the trade this makes on
    purpose: an unpowered bench between runs is the more expensive mistake.

    Best effort, like `_resume_dut_logging`: a hub that cannot be reached here
    is logged rather than raised, or it would replace whatever failure actually
    stopped the scenario.
    """
    global _powered_off
    ports, _powered_off = sorted(_powered_off), set()
    if not ports or switchboard is None:
        return

    LOGGER.info("Restoring power to USB port(s) %s left off by this scenario", ports)
    try:
        switchboard.hub.set_ports(ports, True)
    except Exception as error:  # noqa: BLE001 - must not mask the scenario's own failure
        LOGGER.warning("Could not restore power to USB port(s) %s: %s", ports, error)
