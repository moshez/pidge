"""A fixed session-token signing key and MCP session IDs for tests."""

import dataclasses
import itertools
from collections.abc import Callable

from ..mcpsession import Sessions


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class StaticSecret:
    """A secret that is just a value.

    Attributes:
        secret: The value.
    """

    secret: bytes

    def value(self) -> bytes:
        """Return the secret.

        Returns:
            The value.
        """
        return self.secret


SESSION_KEY = StaticSecret(secret=b"session-key-for-tests-" + b"0" * 42)


BUILD = "0123abc"


def predictable_sessions(log: Callable[[str], None]) -> Sessions:
    """Issue predictable session IDs for build ``BUILD``.

    Args:
        log: Receives the log lines.

    Returns:
        The sessions.
    """
    counter = itertools.count()
    return Sessions(build=BUILD, random=lambda: f"{next(counter):022d}", log=log)
