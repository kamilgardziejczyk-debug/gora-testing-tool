"""Characteristic value encoding.

A GATT characteristic carries raw bytes, but every front end supplies them as
text (a REPL line, a YAML scalar). Encoding lives here, once, so the CLI and
the !BleCentral wrapper accept exactly the same notation.
"""

from __future__ import annotations

# Integer widths are little-endian: that is the byte order the Bluetooth core
# spec mandates for its own numeric characteristic fields, so it is what a
# device's datasheet almost always means.
INT_WIDTHS = {"uint8": 1, "uint16": 2, "uint32": 4}
ENCODINGS = ("hex", "utf8", *INT_WIDTHS)
DEFAULT_ENCODING = "hex"

HEX_SEPARATORS = " :-_"
PRINTABLE_MIN = 0x20
PRINTABLE_MAX = 0x7E


def encode_value(text: str, encoding: str = DEFAULT_ENCODING) -> bytes:
    """Turn `text` into the bytes to write to a characteristic.

    Raises `ValueError` on an unknown encoding or a value that doesn't fit it,
    so a malformed scenario is rejected before any radio traffic happens.
    """
    if encoding == "hex":
        return _encode_hex(text)
    if encoding == "utf8":
        return text.encode("utf-8")
    if encoding in INT_WIDTHS:
        return _encode_int(text, encoding)

    raise ValueError(f"unknown encoding '{encoding}' (expected one of {', '.join(ENCODINGS)})")


def format_value(data: bytes) -> str:
    """Render bytes for a log line or REPL output: hex, plus ASCII when readable.

    The ASCII half is only appended when every byte is printable, so it stays a
    genuine hint rather than a line of dots that hides where the text ended.
    """
    hex_text = data.hex(" ") if data else "(empty)"
    if data and all(PRINTABLE_MIN <= byte <= PRINTABLE_MAX for byte in data):
        return f"{hex_text}  ({data.decode('ascii')!r})"
    return hex_text


def _encode_hex(text: str) -> bytes:
    """Parse hex digits, tolerating the separators datasheets use ('01:ff').

    An empty string encodes to zero bytes, matching `utf8`'s treatment of
    `""` - a deliberate empty write (e.g. clearing a credential
    characteristic) is a legal GATT operation, not a malformed value.
    """
    digits = text
    for separator in HEX_SEPARATORS:
        digits = digits.replace(separator, "")

    if not digits:
        return b""
    if len(digits) % 2 != 0:
        raise ValueError(
            f"hex value must have an even number of digits (got {len(digits)} in '{text}'); "
            f"pad the leading byte, e.g. '0f' rather than 'f'"
        )

    try:
        return bytes.fromhex(digits)
    except ValueError:
        raise ValueError(f"not a hex value: '{text}'") from None


def _encode_int(text: str, encoding: str) -> bytes:
    """Pack an integer into `encoding`'s width, little-endian.

    Unlike `hex`/`utf8`, an empty string has no valid encoding here: `encoding`
    is always exactly `width` byte(s), and there is no integer whose encoding
    is zero bytes. This isn't an inconsistency to fix - it rejects empty for a
    different, unavoidable reason than a malformed value would.
    """
    width = INT_WIDTHS[encoding]
    if not text:
        raise ValueError(
            f"{encoding} has no empty encoding - it is always {width} byte(s) wide; "
            f"use 'hex' or 'utf8' for an empty write"
        )
    try:
        number = int(text, 0)  # base 0 so '0x1f' and '31' both work
    except ValueError:
        raise ValueError(f"not an integer: '{text}'") from None

    try:
        return number.to_bytes(width, byteorder="little", signed=False)
    except OverflowError:
        raise ValueError(
            f"{number} does not fit in {encoding} ({width} byte(s), max {(1 << (8 * width)) - 1})"
        ) from None
