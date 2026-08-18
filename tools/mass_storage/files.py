"""Reading a mounted card: listing, reading and copying files off it.

Every path here is *card-relative* - `/logs` means the card's `logs`
directory, never the host's. Paths come from scenario files, so each one is
resolved and then checked to still be inside the mountpoint: a `path` of
`../../etc` would otherwise read the test node's own filesystem and report it
as the DUT's, which is the kind of wrong answer a test would happily pass on.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .device import MassStorageError
from .mount import READ_WRITE, Mount


LOGGER = logging.getLogger(__name__)

# Files are decoded with replacement rather than strictly: a log the firmware
# truncated mid-write is exactly the thing a test wants to assert about, and
# it must not become an unreadable command instead of a failed assertion.
DECODE_ERRORS = "replace"


class PathEscapesCardError(MassStorageError):
    """A scenario path resolved to somewhere outside the mounted card."""


class CardPathNotFoundError(MassStorageError):
    """A scenario named a path that is not on the card."""


class CardReadOnlyError(MassStorageError):
    """A scenario tried to change a card mounted read-only."""


@dataclass(frozen=True)
class Listing:
    """What one directory on the card holds."""

    path: str
    files: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)

    @property
    def entries(self) -> list[str]:
        """Every name in the directory, files and subdirectories together."""
        return sorted(self.files + self.dirs)


@dataclass(frozen=True)
class FileContent:
    """One file read off the card."""

    path: str
    content: str
    size: int

    @property
    def lines(self) -> list[str]:
        """`content` split into lines, without their line endings."""
        return self.content.splitlines()


def list_entries(mount: Mount, path: str = "/") -> Listing:
    """Names directly under `path` on the card. Not recursive.

    Sorted so a scenario asserting on the whole listing is not at the mercy of
    directory order, which FAT does not define.
    """
    target = resolve(mount, path)
    if not target.is_dir():
        raise CardPathNotFoundError(
            f"'{path}' is not a directory on the card "
            f"({'it is a file' if target.exists() else 'it does not exist'})"
        )

    files = sorted(entry.name for entry in target.iterdir() if entry.is_file())
    dirs = sorted(entry.name for entry in target.iterdir() if entry.is_dir())
    LOGGER.info("Card %s holds %d file(s) and %d directory(ies)", path, len(files), len(dirs))
    return Listing(path=path, files=files, dirs=dirs)


def read_file(mount: Mount, path: str) -> FileContent:
    """Read one file off the card as text."""
    target = resolve(mount, path)
    if not target.is_file():
        raise CardPathNotFoundError(
            f"'{path}' is not a file on the card "
            f"({'it is a directory' if target.exists() else 'it does not exist'})"
        )

    data = target.read_bytes()
    LOGGER.info("Read %s from the card (%d bytes)", path, len(data))
    return FileContent(path=path, content=data.decode("utf-8", errors=DECODE_ERRORS), size=len(data))


def copy_from(mount: Mount, pattern: str, dest: Path) -> list[str]:
    """Copy everything on the card matching `pattern` into `dest`.

    `pattern` may name one file (`/logs/today.log`) or use a glob
    (`/logs/*.log`). Returns the card-relative paths copied, sorted.

    Matching nothing raises rather than returning an empty list: a scenario
    that meant to collect the DUT's logs and collected none has found a
    problem, and reporting success with an empty directory would hide it.
    """
    matches = sorted(_matching(mount, pattern))
    if not matches:
        raise CardPathNotFoundError(f"nothing on the card matches '{pattern}'")

    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for source in matches:
        relative = source.relative_to(mount.mountpoint)
        _copy_one(source, dest / relative.name)
        copied.append(f"/{relative.as_posix()}")

    LOGGER.info("Copied %d item(s) matching '%s' off the card into %s", len(copied), pattern, dest)
    return copied


def delete(mount: Mount, pattern: str) -> list[str]:
    """Delete everything on the card matching `pattern`, and return what went.

    For clearing the card between runs: a scenario that asserts on the logs a
    firmware wrote wants to know they are *this* run's logs, and the cheapest
    way to be sure is to start from a card with none.

    Matching nothing is a **success**, unlike `copy_from`. Clearing a card has
    to be idempotent: the same scenario run twice would otherwise fail the
    second time precisely because the first one worked. A scenario that does
    care can assert on it, since `{count}` is available to `validation`.

    A directory is removed with everything under it. That is the point - a
    firmware writing one directory per session leaves exactly that to clear -
    but it is also why this needs `mode: rw` on the mount, which is opt-in per
    command rather than a scenario-level default.
    """
    if mount.mode != READ_WRITE:
        raise CardReadOnlyError(
            f"the card at {mount.mountpoint} is mounted read-only, so '{pattern}' cannot be "
            f"deleted. Mount it with 'mode: rw' to change anything on the card."
        )

    matches = sorted(_matching(mount, pattern))
    if not matches:
        LOGGER.info("Nothing on the card matches '%s', so there is nothing to delete", pattern)
        return []

    deleted = []
    for target in matches:
        relative = target.relative_to(mount.mountpoint)
        # Logged before the removal, not after: if this fails part way through,
        # the log still says how far it got.
        LOGGER.info("Deleting /%s from the card", relative.as_posix())
        _delete_one(target)
        deleted.append(f"/{relative.as_posix()}")

    # The card can lose power at any point after this - the scenario's whole
    # purpose is to cut it - so the delete is pushed out now rather than left
    # for the unmount to flush.
    os.sync()
    LOGGER.info("Deleted %d item(s) matching '%s' from the card", len(deleted), pattern)
    return deleted


def _delete_one(target: Path) -> None:
    """Remove one file, or one directory and everything under it."""
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def _matching(mount: Mount, pattern: str) -> list[Path]:
    """Card paths matching `pattern`, which may or may not contain a glob."""
    relative = pattern.lstrip("/")
    if not relative:
        # Reachable from `delete` as well as `copy_from`, and rather more
        # important there: the card root is the mountpoint, and removing it
        # would take the mount with it.
        raise ValueError("'path' must name a file, a directory or a glob, not the card root")

    if not any(character in relative for character in "*?["):
        target = resolve(mount, pattern)
        return [target] if target.exists() else []

    # Checked rather than trusted: a glob cannot escape via `..` the way a
    # plain path can, but it can still be written with one.
    return [match for match in mount.mountpoint.glob(relative) if _contains(mount, match)]


def _copy_one(source: Path, destination: Path) -> None:
    """Copy one file or directory tree off the card."""
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(source, destination)


def resolve(mount: Mount, path: str) -> Path:
    """Turn a card-relative `path` into a host path inside the mountpoint.

    Raises `PathEscapesCardError` if it resolves outside, which is the whole
    reason this is a function rather than a `/` join at each call site.
    """
    candidate = (mount.mountpoint / path.lstrip("/")).resolve()
    if not _contains(mount, candidate):
        raise PathEscapesCardError(
            f"'{path}' resolves to {candidate}, which is outside the card mounted at "
            f"{mount.mountpoint}. Scenario paths are relative to the card's root."
        )
    return candidate


def _contains(mount: Mount, candidate: Path) -> bool:
    """Whether `candidate` is the mountpoint or something inside it."""
    root = mount.mountpoint.resolve()
    return candidate == root or root in candidate.resolve().parents
