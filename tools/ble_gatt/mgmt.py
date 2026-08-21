"""Minimal Bluetooth Management (MGMT) client, used only to advertise.

BlueZ's D-Bus `LEAdvertisingManager1` is not usable for this. On every host
tested it rejects `RegisterAdvertisement` with "Invalid Parameters" - including
for BlueZ's own `bluetoothctl` on a freshly restarted adapter, across two
BlueZ versions, two kernels and two controllers. The kernel's MGMT interface
accepts the legacy `Add Advertising` command those same hosts refuse over
D-Bus, so this speaks that interface directly.

Going around BlueZ also fixes a second problem it caused. BlueZ decides for
itself whether the local name travels in the advertisement or the scan
response, and it chooses the scan response - which is invisible to a central
that will only match a peripheral advertising its name and a service UUID
together in one PDU. Here the caller builds the advertising bytes and they are
sent verbatim.

Only the advertising commands are implemented. Everything else the peripheral
needs - the GATT server itself - still goes through `bluetoothd` over D-Bus,
which works, and which must keep owning the adapter.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import socket
import struct
from dataclasses import dataclass
from typing import Optional, Tuple

LOGGER = logging.getLogger(__name__)

# Not exposed by Python's socket module, so they are spelled out here.
AF_BLUETOOTH = 31
BTPROTO_HCI = 1
HCI_CHANNEL_CONTROL = 3
HCI_DEV_NONE = 0xFFFF

# Opcodes read off the wire from btmgmt rather than from memory - the
# advertising block does not sit where the ordering in mgmt-api.txt suggests.
MGMT_OP_READ_ADV_FEATURES = 0x003D
MGMT_OP_ADD_ADVERTISING = 0x003E
MGMT_OP_REMOVE_ADVERTISING = 0x003F

MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002

# Only the one flag is used. Deliberately *not* MGMT_ADV_FLAG_DISCOV or
# MANAGED_FLAGS: those make the kernel prepend its own Flags structure and then
# reject advertising data containing one, and this tool supplies its own so the
# whole PDU stays under the caller's control.
MGMT_ADV_FLAG_CONNECTABLE = 0x0001

_HEADER = struct.Struct("<HHH")
_ADD_ADVERTISING = struct.Struct("<BIHHBB")
_EVENT_PREFIX = struct.Struct("<HB")

DEFAULT_TIMEOUT_S = 5.0
RECV_SIZE = 1024

MGMT_STATUS = {
    0x00: "Success", 0x01: "Unknown Command", 0x02: "Not Connected", 0x03: "Failed",
    0x04: "Connect Failed", 0x05: "Authentication Failed", 0x06: "Not Paired",
    0x07: "No Resources", 0x08: "Timeout", 0x09: "Already Connected", 0x0A: "Busy",
    0x0B: "Rejected", 0x0C: "Not Supported", 0x0D: "Invalid Parameters",
    0x0E: "Disconnected", 0x0F: "Not Powered", 0x10: "Cancelled",
    0x11: "Invalid Index", 0x12: "RFKilled", 0x13: "Already Paired",
    0x14: "Permission Denied",
}


class MgmtError(RuntimeError):
    """The kernel rejected an MGMT command."""


class MgmtUnavailable(RuntimeError):
    """The Bluetooth management socket could not be opened at all."""


@dataclass(frozen=True)
class AdvertisingFeatures:
    """What the adapter can do, and which advertising slots are already taken."""

    supported_flags: int
    max_adv_data_len: int
    max_scan_rsp_len: int
    max_instances: int
    instances: Tuple[int, ...]

    def free_instance(self) -> int:
        """The lowest advertising slot nothing is using.

        Raises `MgmtError` when every slot is taken. Worth a clear message:
        each slot is a finite adapter resource, and a client that fails
        part-way through registering can leave one behind, so exhaustion is a
        real state to end up in rather than a theoretical one.
        """
        for instance in range(1, self.max_instances + 1):
            if instance not in self.instances:
                return instance
        raise MgmtError(
            f"all {self.max_instances} advertising slot(s) on this adapter are in use "
            f"(instances {', '.join(str(i) for i in self.instances)}). Another program is "
            f"advertising, or a previous one left a slot behind - 'systemctl restart "
            f"bluetooth' on the host reclaims them."
        )


def adapter_index(adapter: str) -> int:
    """Turn an adapter name like 'hci0' into the index MGMT addresses it by."""
    name = adapter.strip().lower()
    if not name.startswith("hci") or not name[3:].isdigit():
        raise ValueError(f"not a Bluetooth adapter name: '{adapter}' (expected e.g. 'hci0')")
    return int(name[3:])


class MgmtSocket:
    """A synchronous Bluetooth management socket.

    Usage:

        with MgmtSocket() as mgmt:
            features = mgmt.read_advertising_features(0)
            mgmt.add_advertising(0, features.free_instance(), adv_data)

    Also usable without the context manager, in which case call `close()`.
    """

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S):
        try:
            self._socket = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
            _bind_control_channel(self._socket)
        except OSError as error:
            raise MgmtUnavailable(_unavailable_message(error)) from error
        self._socket.settimeout(timeout_s)
        self.timeout_s = timeout_s

    def __enter__(self) -> "MgmtSocket":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        """Close the socket. Idempotent."""
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def read_advertising_features(self, index: int) -> AdvertisingFeatures:
        """Ask the adapter what it supports and which slots are in use."""
        reply = self._request(MGMT_OP_READ_ADV_FEATURES, index, b"")
        supported_flags, max_adv, max_scan, max_instances, count = struct.unpack_from(
            "<IBBBB", reply, 0
        )
        return AdvertisingFeatures(
            supported_flags=supported_flags,
            max_adv_data_len=max_adv,
            max_scan_rsp_len=max_scan,
            max_instances=max_instances,
            instances=tuple(reply[8 : 8 + count]),
        )

    def add_advertising(
        self,
        index: int,
        instance: int,
        adv_data: bytes,
        scan_rsp: bytes = b"",
        flags: int = MGMT_ADV_FLAG_CONNECTABLE,
    ) -> int:
        """Start advertising `adv_data` verbatim, and return the instance used.

        `duration` and `timeout` are both zero: the advertisement runs until it
        is removed, which is what a simulated sensor wants - a timeout would
        make it vanish mid-test for no reason the scenario could see.
        """
        payload = _ADD_ADVERTISING.pack(
            instance, flags, 0, 0, len(adv_data), len(scan_rsp)
        ) + adv_data + scan_rsp

        reply = self._request(MGMT_OP_ADD_ADVERTISING, index, payload)
        LOGGER.info(
            "Advertising instance %d added on hci%d (%d byte(s) of advertising data)",
            instance, index, len(adv_data),
        )
        return reply[0] if reply else instance

    def remove_advertising(self, index: int, instance: int) -> None:
        """Stop an advertisement. Instance 0 removes every one this socket owns."""
        self._request(MGMT_OP_REMOVE_ADVERTISING, index, struct.pack("<B", instance))
        LOGGER.info("Advertising instance %d removed on hci%d", instance, index)

    def _request(self, opcode: int, index: int, payload: bytes) -> bytes:
        """Send one command and wait for the reply that belongs to it.

        The socket also carries unsolicited events, so replies are matched on
        the command opcode rather than assuming the next packet is ours.
        """
        if self._socket is None:
            raise MgmtError("management socket is closed")

        self._socket.send(_HEADER.pack(opcode, index, len(payload)) + payload)
        while True:
            try:
                packet = self._socket.recv(RECV_SIZE)
            except socket.timeout:
                raise MgmtError(
                    f"no reply to management command 0x{opcode:04x} within {self.timeout_s}s"
                ) from None

            event, _, length = _HEADER.unpack_from(packet, 0)
            body = packet[_HEADER.size : _HEADER.size + length]
            if event not in (MGMT_EV_CMD_COMPLETE, MGMT_EV_CMD_STATUS):
                continue

            command, status = _EVENT_PREFIX.unpack_from(body, 0)
            if command != opcode:
                continue
            if status != 0:
                raise MgmtError(
                    f"management command 0x{opcode:04x} was rejected: "
                    f"{MGMT_STATUS.get(status, 'unknown')} (0x{status:02x})"
                )
            return body[_EVENT_PREFIX.size :]


def _bind_control_channel(sock: socket.socket) -> None:
    """Bind `sock` to the management control channel.

    The address is built by hand and handed to libc rather than going through
    `socket.bind`, which only learned to accept the channel field of
    sockaddr_hci in Python 3.12: earlier versions parse a single device id and
    reject the two-element form with "wrong format". The container this runs in
    is on 3.11 while a developer machine may be on 3.14, so doing it this way
    keeps one code path that behaves the same on both.
    """
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    # struct sockaddr_hci: family, device id, channel - three little-endian
    # unsigned shorts.
    address = struct.pack("<HHH", AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_CONTROL)
    if libc.bind(sock.fileno(), address, len(address)) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def _unavailable_message(error: OSError) -> str:
    """Explain a failure to open the socket in terms of what to change."""
    if error.errno == 97:  # EAFNOSUPPORT
        return (
            "the Bluetooth management socket is not available in this network namespace. "
            "Bluetooth sockets are scoped to a network namespace, so a container on the "
            "default bridge network cannot see the host's adapter: run it with "
            "--net=host. (This is separate from the D-Bus socket the GATT server needs.)"
        )
    if error.errno in (1, 13):  # EPERM, EACCES
        return (
            "not permitted to open the Bluetooth management socket. It needs CAP_NET_ADMIN - "
            "run as root, or give the container --cap-add=NET_ADMIN."
        )
    return f"could not open the Bluetooth management socket: {error}"
