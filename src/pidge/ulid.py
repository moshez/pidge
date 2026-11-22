"""Universally unique lexicographically sortable identifiers (ULIDs)."""

import re
from collections.abc import Callable

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_PATTERN = re.compile(r"\A[0-9A-HJKMNP-TV-Z]{26}\Z")


def encode(milliseconds: int, randomness: int) -> str:
    """Encode a timestamp and random bits as a ULID.

    Args:
        milliseconds: Milliseconds since the epoch (48 bits).
        randomness: Random value (80 bits).

    Returns:
        The 26-character Crockford base32 ULID.
    """
    value = (milliseconds << 80) | randomness
    chars = []
    for _ in range(26):
        chars.append(ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid(clock: Callable[[], float], randbits: Callable[[int], int]) -> str:
    """Allocate a new ULID.

    Args:
        clock: Returns the current time in seconds since the epoch.
        randbits: Returns a random integer with the given number of bits.

    Returns:
        A fresh ULID.
    """
    return encode(int(clock() * 1000), randbits(80))


def timestamp(ulid: str) -> float:
    """Extract the creation time from a ULID.

    Args:
        ulid: The ULID.

    Returns:
        Seconds since the epoch.
    """
    value = 0
    for char in ulid[:10]:
        value = value * 32 + ALPHABET.index(char)
    return value / 1000


def is_ulid(text: str) -> bool:
    """Check whether a string is a well-formed ULID.

    Args:
        text: The candidate string.

    Returns:
        True if it is a ULID.
    """
    return _PATTERN.match(text) is not None
