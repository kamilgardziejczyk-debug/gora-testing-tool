"""The DUT's SD card, reached over USB mass storage.

Runnable from the command line (see `mass_storage.py`) for bring-up checks,
and importable as an API, which is how the `!DutStorage` wrapper drives it.

The card is mounted the way any host would mount it, rather than read as raw
blocks, because the firmware's hand-off of the card is itself under test - see
`mount.py` and `docs/usb-mass-storage-design.md`.
"""

from .device import (
    DEFAULT_SETTLE_TIMEOUT_S,
    BlockDevice,
    BlockDeviceNotFoundError,
    MassStorageError,
    find_block_device,
    usb_device_path,
)
from .files import (
    CardPathNotFoundError,
    CardReadOnlyError,
    FileContent,
    Listing,
    PathEscapesCardError,
    copy_from,
    delete,
    list_entries,
    read_file,
)
from .mount import (
    DEFAULT_MOUNT_ROOT,
    DEFAULT_TIMEOUT_S,
    MODES,
    READ_ONLY,
    READ_WRITE,
    CommandError,
    Mount,
    MountError,
    ToolNotFoundError,
    default_mountpoint,
    eject,
    filesystem_type,
    is_mounted,
    mount,
    unmount,
)

__all__ = [
    "CardPathNotFoundError",
    "CardReadOnlyError",
    "CommandError",
    "DEFAULT_MOUNT_ROOT",
    "DEFAULT_SETTLE_TIMEOUT_S",
    "DEFAULT_TIMEOUT_S",
    "MODES",
    "READ_ONLY",
    "READ_WRITE",
    "BlockDevice",
    "BlockDeviceNotFoundError",
    "FileContent",
    "Listing",
    "MassStorageError",
    "Mount",
    "MountError",
    "PathEscapesCardError",
    "ToolNotFoundError",
    "copy_from",
    "default_mountpoint",
    "delete",
    "eject",
    "filesystem_type",
    "find_block_device",
    "is_mounted",
    "list_entries",
    "mount",
    "read_file",
    "unmount",
    "usb_device_path",
]
