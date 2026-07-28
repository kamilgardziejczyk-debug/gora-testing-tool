"""GATT UUID normalization.

Every UUID that crosses this package's API is normalized to its full 128-bit
form, so a scenario can write the 16-bit shorthand a datasheet uses ("180a")
and still match what the adapter reports.
"""

from __future__ import annotations

from bleak.uuids import normalize_uuid_str


def normalize_uuid(value: str) -> str:
    """Expand a 16-, 32- or 128-bit GATT UUID to its lowercase 128-bit form.

    Accepts a leading `0x` and surrounding whitespace, which datasheets and
    hand-written YAML both tend to include. Raises `ValueError` with the
    offending value on anything else - bleak's own message doesn't name it,
    which makes a typo in a long scenario hard to find.
    """
    text = value.strip().lower()
    if text.startswith("0x"):
        text = text[2:]

    try:
        return normalize_uuid_str(text)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(
            f"not a valid GATT UUID: '{value}'. Expected 16-bit shorthand like '180a', "
            f"32-bit like '0000180a', or a full 128-bit UUID."
        ) from None
