r"""!DutStorage - mount the DUT's SD card over USB and assert on what is on it.

    - !DutStorage:
      name: "Mount The Card"
      port: dut_storage
      action: mount

    - !DutStorage:
      name: "Card Holds Today's Log"
      action: list
      path: "/logs"
      validation: "'tracker-2026-08-18.log' in {files}"

The card is mounted the way any host would mount it rather than read as raw
blocks, because the firmware's hand-off of the card is itself under test. The
tracker gives the card to the host by unmounting it from its own filesystem,
and takes it back in two steps: an eject returns control to the ESP32, and
only the later loss of VBUS makes it re-mount the card for its own use.

`mount`, `unmount` and `eject` are therefore separate commands rather than
something the file actions do implicitly - they are the steps being tested.
Assert on what the firmware made of them with !DutLogExpect, since none of it
is visible from the host side.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from tools.mass_storage import (
    DEFAULT_SETTLE_TIMEOUT_S,
    MODES,
    READ_ONLY,
    READ_WRITE,
    BlockDevice,
    Mount,
    copy_from,
    default_mountpoint,
    delete,
    eject,
    find_block_device,
    list_entries,
    mount,
    read_file,
    unmount,
)
from tools.usb_hub import Switchboard

from .expression import Expression, compile_expression
from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

MOUNT = "mount"
UNMOUNT = "unmount"
EJECT = "eject"
LIST = "list"
READ = "read"
COPY_FROM = "copy_from"
DELETE = "delete"
ACTIONS = (MOUNT, UNMOUNT, EJECT, LIST, READ, COPY_FROM, DELETE)

# Variables each action offers a `validation` expression. The lifecycle
# actions offer none: what the DUT made of them shows up on its console, not
# in anything readable from the host.
VARIABLES: dict[str, tuple[str, ...]] = {
    MOUNT: (),
    UNMOUNT: (),
    EJECT: (),
    LIST: ("files", "dirs", "entries"),
    READ: ("content", "lines", "size"),
    COPY_FROM: ("copied", "count"),
    DELETE: ("deleted", "count"),
}

# Actions that need a card already mounted by an earlier command.
NEEDS_MOUNT = (LIST, READ, COPY_FROM, DELETE)

# Actions that change the card, and so need it mounted `rw`.
WRITES_TO_CARD = (DELETE,)

# The card this scenario has mounted, so a later command can use it. Module
# level for the same reason usb_switch_wrapper's `_powered_off` is: the Parser
# builds a fresh wrapper per command and has nowhere to hang state that
# outlives one, and a mount belongs to the whole run.
_mounted: Mount | None = None


class DutStorageWrapper(Wrapper):
    """One operation on the DUT's SD card, exposed over USB mass storage.

    `action` selects the operation, and each one binds its own variables for
    `validation` to assert on:

    `list` - names directly under `path` (not recursive):
        {files}    list[str]  file names, sorted
        {dirs}     list[str]  subdirectory names, sorted
        {entries}  list[str]  both, sorted

    There is deliberately no {count} here: with both {files} and {dirs} in
    scope it could only be ambiguous, and `len({dirs})` or
    `len(matching("session_*", {dirs}))` says which count is meant. The
    actions that produce a single list do offer it.

    `read` - the single file at `path`:
        {content}  str        the file decoded as text
        {lines}    list[str]  {content} split on newlines, no line endings
        {size}     int        the file's size in bytes, not characters

    `copy_from` - what was copied off the card into `dest`:
        {copied}   list[str]  card-relative paths copied, sorted
        {count}    int        len({copied})

    `delete` - what was removed from the card:
        {deleted}  list[str]  card-relative paths deleted, sorted
        {count}    int        len({deleted})

    `mount`, `unmount` and `eject` bind no variables and take no `validation`.
    `eject` in particular is the only action the firmware observes: a plain
    unmount never reaches it, because Linux `umount` sends no SCSI command.

    `delete` is the one action that changes the card, so it needs the mount
    to have been made with `mode: rw`. Matching nothing is a success rather
    than a failure, unlike `copy_from`: clearing a card has to be idempotent
    or the same scenario would fail on its second run precisely because the
    first one worked.

    Mounts read-only unless `mode: rw`, which is opt-in per command rather
    than a scenario-level default - a wrong `path` under `rw` writes to the
    DUT's card. The runner unmounts anything still mounted when the scenario
    ends: a card left mounted when !UsbSwitch cuts VBUS wedges the kernel on a
    device that no longer exists, which outlives the run and takes the next
    one with it.
    """

    requires_usb_hub = True

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.action: str | None = None
        self.usb_port: str | None = None
        self.path: str | None = None
        self.dest: str | None = None
        self.mode: str = READ_ONLY
        self.settle_timeout_s: float = DEFAULT_SETTLE_TIMEOUT_S
        self.validation: str | None = None
        self.expression: Expression | None = None

    def parse(self) -> None:
        """Read the command's YAML fields and reject an unusable combination."""
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "DutStorage":
            raise ValueError("Expected !DutStorage command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue
            self._set_field(key_node.value, value_node.value)

        self._validate()

        LOGGER.info(
            "Parsed DutStorage: name=%s, action=%s, port=%s, path=%s, validation=%s",
            self.name,
            self.action,
            self.usb_port,
            self.path,
            self.validation,
        )

    def _set_field(self, key: str, value: str) -> None:
        """Apply one YAML field to this command."""
        if key == "name":
            self.name = value
        elif key == "action":
            self.action = value.strip()
        elif key == "port":
            self.usb_port = value
        elif key == "path":
            self.path = value
        elif key == "dest":
            self.dest = value
        elif key == "mode":
            self.mode = value.strip().lower()
        elif key == "settle_timeout_s":
            self.settle_timeout_s = float(value)
        elif key == "validation":
            self.validation = value

    def _validate(self) -> None:
        """Fail at parse time on anything checkable without the card attached."""
        if self.action is None:
            raise ValueError(f"DutStorage: 'action' is required, one of {', '.join(ACTIONS)}")
        if self.action not in ACTIONS:
            raise ValueError(
                f"DutStorage: unknown action '{self.action}' (expected one of {', '.join(ACTIONS)})"
            )
        if self.mode not in MODES:
            raise ValueError(f"DutStorage: 'mode' must be one of {', '.join(MODES)}, got '{self.mode}'")
        if self.settle_timeout_s <= 0:
            raise ValueError(
                f"DutStorage: settle_timeout_s must be > 0, got {self.settle_timeout_s}"
            )

        self._validate_action_fields()
        self._compile_validation()

    def _validate_action_fields(self) -> None:
        """Reject a field this action cannot use, or a required one left out."""
        if self.action in (MOUNT, EJECT) and self.usb_port is None:
            raise ValueError(
                f"DutStorage: '{self.action}' needs 'port' - a port number, or a name from the "
                f"scenario's usb_hub.ports block"
            )
        if self.action in (READ, COPY_FROM, DELETE) and self.path is None:
            raise ValueError(f"DutStorage: '{self.action}' needs 'path'")
        if self.action == COPY_FROM and self.dest is None:
            raise ValueError("DutStorage: 'copy_from' needs 'dest', a directory to copy into")
        if self.action != COPY_FROM and self.dest is not None:
            raise ValueError(f"DutStorage: 'dest' means nothing to '{self.action}'")
        if self.action != MOUNT and self.mode != READ_ONLY:
            raise ValueError(f"DutStorage: 'mode' means nothing to '{self.action}'")

    def _compile_validation(self) -> None:
        """Compile `validation`, rejecting one on an action that asserts nothing."""
        if self.validation is None:
            return
        allowed = VARIABLES[self.action]
        if not allowed:
            raise ValueError(
                f"DutStorage: '{self.action}' has nothing to assert about, so it takes no "
                f"'validation'. What the DUT made of it appears on its console - assert on "
                f"that with !DutLogExpect."
            )
        self.expression = compile_expression(
            self.validation, allowed, f"DutStorage: '{self.action}' validation"
        )

    def execute(self) -> None:
        """Run this command's action against the card."""
        if self.action == MOUNT:
            self._mount()
        elif self.action == UNMOUNT:
            self._unmount()
        elif self.action == EJECT:
            self._eject()
        else:
            self._run_file_action(_require_mounted(self.action, self.action in WRITES_TO_CARD))

    def _mount(self) -> None:
        """Find the card on this command's port and mount it."""
        global _mounted
        if _mounted is not None:
            raise ValueError(
                f"DutStorage: the card is already mounted at {_mounted.mountpoint}. Unmount it "
                f"before mounting again."
            )
        device = self._find_device()
        _mounted = mount(device, default_mountpoint(device), self.mode)
        LOGGER.info("DutStorage: mounted %s", _mounted)

    def _unmount(self) -> None:
        """Unmount the card this scenario mounted, if it still is."""
        global _mounted
        if _mounted is None:
            LOGGER.info("DutStorage: nothing is mounted, so there is nothing to unmount")
            return
        unmount(_mounted.mountpoint)
        _mounted = None

    def _eject(self) -> None:
        """Tell the DUT to take its card back.

        Refuses while the card is still mounted: ejecting a mounted filesystem
        leaves the kernel holding one whose device has gone, which is the
        failure this wrapper works hardest to avoid.
        """
        if _mounted is not None:
            raise ValueError(
                f"DutStorage: the card is still mounted at {_mounted.mountpoint}. Unmount it "
                f"before ejecting, or the kernel is left with a filesystem whose device has gone."
            )
        eject(self._find_device())

    def _find_device(self) -> BlockDevice:
        """The block device on this command's hub port."""
        switchboard = self._require_switchboard()
        port = switchboard.resolve(self.usb_port)
        return find_block_device(switchboard.hub.location, port, self.settle_timeout_s)

    def _run_file_action(self, card: Mount) -> None:
        """Run a read-only card action and check its `validation`."""
        variables = self._collect(card)
        if self.expression is None:
            return

        passed = self.expression.evaluate(variables)
        # Set either way, so the report shows both sides of the assertion.
        self.validation_expected = f"card {self.action} {self.path or '/'}: {self.validation}"
        self.validation_actual = self.expression.describe(variables)

        if not passed:
            raise ValueError(
                f"DutStorage: the card did not satisfy '{self.validation}' "
                f"({self.expression.describe(variables)})"
            )
        LOGGER.info("DutStorage: card satisfied '%s'", self.validation)

    def _collect(self, card: Mount) -> dict[str, object]:
        """Perform this action and bind the variables it offers."""
        if self.action == LIST:
            listing = list_entries(card, self.path or "/")
            return {
                "files": listing.files,
                "dirs": listing.dirs,
                "entries": listing.entries,
            }
        if self.action == READ:
            content = read_file(card, self.path)
            return {"content": content.content, "lines": content.lines, "size": content.size}

        if self.action == COPY_FROM:
            copied = copy_from(card, self.path, self._resolve_dest())
            return {"copied": copied, "count": len(copied)}

        deleted = delete(card, self.path)
        return {"deleted": deleted, "count": len(deleted)}

    def _resolve_dest(self) -> Path:
        """`dest` as an absolute path, relative to the scenario's directory.

        Resolved the way every other path-taking tag resolves one, so a
        scenario stays portable between the repo and a test node.
        """
        destination = Path(self.dest)
        if destination.is_absolute() or self.scenario_dir is None:
            return destination
        return self.scenario_dir / destination

    def _require_switchboard(self) -> Switchboard:
        """The run's switchboard, or the misconfiguration that explains its absence."""
        if self.usb_switchboard is None:
            raise RuntimeError(
                "DutStorage: no USB hub is attached to this run. This is a wiring mistake in "
                "the runner, not in the scenario - the switchboard is built for every "
                "scenario containing a !DutStorage command."
            )
        return self.usb_switchboard


def _require_mounted(action: str, writable: bool = False) -> Mount:
    """The scenario's mounted card, or an explanation of why it cannot be used.

    `writable` additionally requires the mount to have been made `rw`, checked
    here rather than left to the kernel so a scenario gets "mount it rw" back
    instead of a read-only filesystem errno from somewhere inside shutil.
    """
    if _mounted is None:
        raise ValueError(
            f"DutStorage: '{action}' needs the card mounted, but no !DutStorage command has "
            f"mounted it yet. Add an 'action: mount' command first - mounting is a step the "
            f"DUT observes, so it is never done implicitly."
        )
    if writable and _mounted.mode != READ_WRITE:
        raise ValueError(
            f"DutStorage: '{action}' changes the card, but it was mounted read-only. Add "
            f"'mode: rw' to the 'action: mount' command that mounted it."
        )
    return _mounted


def restore_all() -> None:
    """Unmount any card this scenario left mounted.

    Called from the scenario runner's `finally`, mirroring the relay and USB
    port cleanups. A run that fails between a `mount` and its `unmount` would
    otherwise leave the filesystem mounted, and the !UsbSwitch cleanup that
    follows cuts the port's power underneath it - leaving the kernel with a
    mount whose device has vanished, which wedges anything that touches it and
    outlives this run.

    Falls back to a lazy unmount, and then to logging: this must not replace
    whatever failure actually stopped the scenario.
    """
    global _mounted
    card, _mounted = _mounted, None
    if card is None:
        return

    LOGGER.info("Unmounting the card left mounted at %s by this scenario", card.mountpoint)
    try:
        unmount(card.mountpoint)
        return
    except Exception as error:  # noqa: BLE001 - must not mask the scenario's own failure
        LOGGER.warning("Could not unmount %s (%s); trying a lazy unmount", card.mountpoint, error)

    try:
        unmount(card.mountpoint, lazy=True)
    except Exception as error:  # noqa: BLE001 - same
        LOGGER.warning("Could not unmount %s at all: %s", card.mountpoint, error)
