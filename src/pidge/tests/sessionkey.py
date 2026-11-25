"""A fixed session-token signing key for tests."""

import dataclasses


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
