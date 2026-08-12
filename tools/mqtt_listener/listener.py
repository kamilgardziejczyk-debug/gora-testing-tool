"""MQTT listener: connects over mutual TLS and buffers everything it receives.

Buffering starts the moment `subscribe()` returns, so a later reader still sees
publishes that happened before it started reading. That is what lets a test
trigger an action first and inspect the resulting messages afterwards, without
racing the device.

This module only receives and buffers. Deciding whether a message is the one a
test was waiting for is the caller's job.

Given a `log_session`, everything it receives is also written to that run's
`.mqtt.log` and `.combined.log` as it arrives - a record of what the broker
actually carried, independent of which messages a check happened to consume.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, NamedTuple

import paho.mqtt.client as mqtt

if TYPE_CHECKING:  # imported for typing only, keeping this tool usable on its own
    from tools.dut_logger import LogSession

LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 8883
DEFAULT_QOS = 1
DEFAULT_CONNECT_TIMEOUT_S = 10.0
KEEPALIVE_S = 60
SUBACK_TIMEOUT_S = 10.0
POLL_INTERVAL_S = 0.5
MAX_PENDING_MESSAGES = 1000
MAX_HISTORY_MESSAGES = 100


def _single_line(payload: str) -> str:
    """`payload` with its line breaks escaped, so it occupies one log line."""
    return payload.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


class Message(NamedTuple):
    """A single received MQTT message, payload already decoded to text."""

    topic: str
    payload: str


class MqttListener:
    """A live MQTT connection that buffers messages on its subscriptions.

    Usage:

        listener = MqttListener("host", 8883, "id", "cert.pem", "key.pem", "ca.pem")
        listener.connect()
        listener.subscribe("devices/+/telemetry")
        for message in listener.stream(duration_s=30):
            print(message.topic, message.payload)
        listener.close()

    Also usable as a context manager, which closes the connection on exit.
    """

    def __init__(
        self,
        endpoint: str,
        port: int,
        client_id: str,
        cert: str,
        private_key: str,
        root_ca: str,
        log_session: LogSession | None = None,
    ):
        self.endpoint = endpoint
        self.port = port
        self.client_id = client_id
        self._log_session = log_session

        self._messages: queue.Queue = queue.Queue(maxsize=MAX_PENDING_MESSAGES)
        self._history: deque = deque(maxlen=MAX_HISTORY_MESSAGES)
        self._connected = threading.Event()
        self._subscribed = threading.Event()
        self._connect_failure: str | None = None
        self._dropped = 0
        self._closed = False

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self._client.tls_set(ca_certs=root_ca, certfile=cert, keyfile=private_key)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message

    def __enter__(self) -> "MqttListener":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def connect(self, timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S) -> None:
        """Open the TLS connection and block until the broker acknowledges it."""
        LOGGER.info("Connecting to MQTT broker %s:%s as %s", self.endpoint, self.port, self.client_id)
        self._log_mqtt(f"connect  -> {self.endpoint}:{self.port}")
        try:
            self._client.connect(self.endpoint, self.port, keepalive=KEEPALIVE_S)
        except OSError as error:  # socket errors and TLS handshake failures
            self._log_mqtt(f"connect  !! TLS connection failed ({error})")
            raise ConnectionError(
                f"could not establish a TLS connection to {self.endpoint}:{self.port} ({error})"
            ) from error

        self._client.loop_start()

        if not self._connected.wait(timeout_s):
            self._client.loop_stop()
            self._log_mqtt(f"connect  !! no CONNACK within {timeout_s}s")
            raise ConnectionError(f"no CONNACK from {self.endpoint} within {timeout_s}s")

        if self._connect_failure is not None:
            self.close()
            raise ConnectionError(
                f"broker refused the connection for client_id={self.client_id} "
                f"({self._connect_failure}). Check the IoT policy allows this client id."
            )

    def subscribe(self, topic: str, qos: int = DEFAULT_QOS) -> None:
        """Subscribe and block until the broker confirms it with a SUBACK."""
        self._subscribed.clear()
        result, _ = self._client.subscribe(topic, qos=qos)
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"subscribe to '{topic}' failed with code {result}")

        # Wait for the SUBACK rather than just the send: until the broker has
        # registered the subscription a publish can still slip past us, which is
        # the race this listener exists to close.
        if not self._subscribed.wait(SUBACK_TIMEOUT_S):
            raise ConnectionError(f"no SUBACK for '{topic}' within {SUBACK_TIMEOUT_S}s")

        LOGGER.info("Subscribed to '%s' (qos=%d), buffering messages", topic, qos)
        self._log_mqtt(f"sub      -> {topic} (qos={qos})")

    def stream(self, duration_s: float | None = None):
        """Yield messages as they arrive, stopping after `duration_s` if given."""
        deadline = time.monotonic() + duration_s if duration_s is not None else float("inf")
        while True:
            message = self._next_message(deadline)
            if message is None:
                return
            yield message

    def recent(self) -> list:
        """Every message seen on this session, newest last, for diagnostics."""
        return list(self._history)

    def requeue(self, message: Message) -> None:
        """Put a message back for a later reader.

        For a consumer that only wants messages matching one topic filter out
        of several subscribed on this session: it still has to pop everything
        via `stream()` to see what's arrived, but anything that isn't its own
        needs to go back so a later reader (e.g. another topic's check) still
        sees it. Not re-added to the `recent()` history, since it was already
        recorded there when first received.
        """
        try:
            self._messages.put_nowait(message)
        except queue.Full:
            self._dropped += 1

    def close(self) -> None:
        """Close the connection and stop the network thread. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._client.disconnect()
        self._client.loop_stop()
        LOGGER.info("Disconnected MQTT client %s from %s", self.client_id, self.endpoint)
        self._log_mqtt(f"close    -- disconnected from {self.endpoint}")

    def _log_mqtt(self, line: str) -> None:
        """Write one line to the run's MQTT log, if this listener was given one.

        Every line carries the `client_id`, since one run's log holds every
        session's traffic and a scenario can have several open at once.
        """
        if self._log_session is not None:
            self._log_session.write_mqtt(f"{self.client_id}  {line}")

    def _next_message(self, deadline: float) -> Message | None:
        """Pop the next buffered message, or None once `deadline` has passed.

        `deadline` is an absolute `time.monotonic()` value so that a caller
        looping over non-matching messages cannot extend its own budget. It may
        be `inf` to wait indefinitely.

        The wait is split into POLL_INTERVAL_S slices rather than blocking for
        the whole remaining time: an infinite timeout overflows the platform's
        time_t, and short waits keep Ctrl-C responsive on every platform.
        """
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                return self._messages.get(timeout=min(remaining, POLL_INTERVAL_S))
            except queue.Empty:
                continue

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            self._connect_failure = str(reason_code)
            LOGGER.error("MQTT connect refused for client_id=%s: %s", self.client_id, reason_code)
            self._log_mqtt(f"connect  !! refused by broker ({reason_code})")
        else:
            LOGGER.info("MQTT connected to %s:%s as %s", self.endpoint, self.port, self.client_id)
            self._log_mqtt(f"connect  <- CONNACK from {self.endpoint}")
        self._connected.set()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        if self._closed:
            return
        LOGGER.warning(
            "MQTT connection to %s dropped unexpectedly (%s). Note that a second client "
            "connecting with client_id=%s will evict this one.",
            self.endpoint,
            reason_code,
            self.client_id,
        )
        self._log_mqtt(f"lost     !! connection dropped unexpectedly ({reason_code})")

    def _on_subscribe(self, client, userdata, mid, reason_code_list, properties) -> None:
        self._subscribed.set()

    def _on_message(self, client, userdata, message) -> None:
        entry = Message(message.topic, message.payload.decode("utf-8", errors="replace"))
        LOGGER.info("MQTT %s <- %s  %s", self.client_id, entry.topic, entry.payload)
        # One log line per message, whatever the payload: a pretty-printed JSON
        # shadow update would otherwise span lines in the combined log and read
        # as if the DUT or the tool had said it.
        self._log_mqtt(f"rx       <- {entry.topic}  {_single_line(entry.payload)}")
        self._history.append(entry)
        try:
            self._messages.put_nowait(entry)
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1:
                LOGGER.warning(
                    "MQTT buffer full at %d messages, dropping further ones",
                    MAX_PENDING_MESSAGES,
                )
                self._log_mqtt(
                    f"drop     !! buffer full at {MAX_PENDING_MESSAGES} messages, "
                    f"dropping further ones (they stay in this log)"
                )
