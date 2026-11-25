"""Short text for list rows and headers: names, initials, snippets."""

import email.utils
import re

SNIPPET_CHARS = 140


def display_name(address: str) -> str:
    """Shorten ``Name <addr>`` to ``Name``; leave a bare address alone.

    Args:
        address: The address.

    Returns:
        The name to show.
    """
    name, addr = email.utils.parseaddr(address)
    return name or addr or address


def bare_address(address: str) -> str:
    """Strip the display name from an address.

    Args:
        address: The address.

    Returns:
        The bare address.
    """
    return email.utils.parseaddr(address)[1] or address


def initials(name: str) -> str:
    """Make an avatar's initials.

    Args:
        name: The display name.

    Returns:
        Up to two upper-case initials, or ``?``.
    """
    words = re.findall(r"[A-Za-z0-9]+", name)
    return "".join(word[0] for word in words[:2]).upper() or "?"


def snippet(text: str) -> str:
    """Flatten and shorten text for a list row.

    Args:
        text: The text.

    Returns:
        At most 140 characters, ending in an ellipsis if cut.
    """
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= SNIPPET_CHARS:
        return flat
    return flat[: SNIPPET_CHARS - 1].rstrip() + "…"
