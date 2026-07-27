"""Registry of live MQTT listeners, keyed by the scenario's `session` name.

This is module-level state, which the rest of the codebase avoids. It is here
because `!MqttSubscribe` has to start buffering *before* the command that
triggers a publish runs, while a later command reads from that same listener.
The Parser builds every wrapper independently and has no way to hand a reference
from one to another, so they rendezvous through this registry instead.

No locking: scenario commands execute sequentially on one thread. The listener's
own network thread never touches the registry.
"""

import logging

from tools.mqtt_listener import MqttListener

LOGGER = logging.getLogger(__name__)

_SESSIONS: dict[str, MqttListener] = {}


def register(name: str, listener: MqttListener) -> None:
    """Store a connected listener under `name` for later commands to read."""
    if name in _SESSIONS:
        raise ValueError(
            f"MQTT: session '{name}' is already open. Give this subscription a "
            f"different session name, or close the first with !MqttDisconnect."
        )
    _SESSIONS[name] = listener
    LOGGER.info("Opened MQTT session '%s'", name)


def get(name: str) -> MqttListener:
    """Look up an open listener, raising if the scenario never opened it."""
    listener = _SESSIONS.get(name)
    if listener is None:
        known = ", ".join(sorted(_SESSIONS)) or "none"
        raise ValueError(
            f"MQTT: no open session named '{name}' (open sessions: {known}). Add a "
            f'!MqttSubscribe with session: "{name}" earlier in the scenario.'
        )
    return listener


def pop(name: str) -> MqttListener | None:
    """Remove a listener from the registry, returning it if it was open."""
    return _SESSIONS.pop(name, None)


def close_all() -> None:
    """Disconnect every open listener. Safe to call when none are open.

    Called from the scenario runner's `finally`, so a command that raises
    part-way through still leaves the broker connections closed cleanly.
    """
    for name, listener in list(_SESSIONS.items()):
        LOGGER.info("Closing MQTT session '%s'", name)
        try:
            listener.close()
        except Exception:  # noqa: BLE001 - cleanup must not mask the real error
            LOGGER.exception("Failed to close MQTT session '%s'", name)
    _SESSIONS.clear()
