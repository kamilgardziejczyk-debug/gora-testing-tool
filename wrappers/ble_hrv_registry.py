"""Registry of running heart rate sensor simulations, keyed by `session` name.

Module-level state, which the rest of the codebase avoids. It is here for the
same reason `mqtt_registry` is: a simulated sensor has to keep advertising and
streaming *while other commands run* - that is the whole point of it - so the
command that starts it and the commands that later drive, check and stop it are
different commands. The Parser builds every wrapper independently and has no
way to hand a reference from one to another, so they rendezvous here.

No locking: scenario commands execute sequentially on one thread. Each
simulator's own streaming thread never touches the registry.
"""

import logging

from tools.ble_gatt.hr_simulator import HeartRateSimulator

LOGGER = logging.getLogger(__name__)

_SESSIONS: dict[str, HeartRateSimulator] = {}


def register(name: str, simulator: HeartRateSimulator) -> None:
    """Store a running simulator under `name` for later commands to reach."""
    if name in _SESSIONS:
        raise ValueError(
            f"BleHrvSim: session '{name}' is already running. Give this one a different "
            f"session name, or stop the first with !BleHrvSimStop."
        )
    _SESSIONS[name] = simulator
    LOGGER.info("Opened BLE HRV session '%s'", name)


def get(name: str) -> HeartRateSimulator:
    """Look up a running simulator, raising if the scenario never started it."""
    simulator = _SESSIONS.get(name)
    if simulator is None:
        known = ", ".join(sorted(_SESSIONS)) or "none"
        raise ValueError(
            f"BleHrvSim: no running session named '{name}' (running: {known}). Add a "
            f'!BleHrvSimStart with session: "{name}" earlier in the scenario.'
        )
    return simulator


def pop(name: str) -> HeartRateSimulator | None:
    """Remove a simulator from the registry, returning it if it was running."""
    return _SESSIONS.pop(name, None)


def close_all() -> None:
    """Stop every running simulator. Safe to call when none are running.

    Called from the scenario runner's `finally`, so a command that raises
    part-way through still leaves the adapter free of a stale advertisement -
    which would otherwise keep a DUT connected to a sensor nobody is driving.
    """
    for name, simulator in list(_SESSIONS.items()):
        LOGGER.info("Closing BLE HRV session '%s'", name)
        try:
            simulator.close()
        except Exception:  # noqa: BLE001 - cleanup must not mask the real error
            LOGGER.exception("Failed to close BLE HRV session '%s'", name)
    _SESSIONS.clear()
