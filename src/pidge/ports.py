"""Interfaces and settings shared by every flow."""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from .store import ObjectStore


class SendRejected(Exception):
    """SES refused a message permanently (for example, sandbox limits)."""


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Envelope:
    """Who an outgoing message goes to, and how SES should track it.

    Attributes:
        recipients: Every address to deliver to (To, Cc and Bcc).
        tags: SES message tags, echoed back in SES events.
    """

    recipients: Sequence[str]
    tags: Mapping[str, str]


class Sender(Protocol):
    """Sends raw MIME through SES."""

    def send(self, raw: bytes, envelope: Envelope) -> str:
        """Send a message.

        Args:
            raw: The MIME bytes.
            envelope: Recipients and tags.

        Returns:
            The SES message ID; raises SendRejected on a permanent refusal.
        """


class Invoker(Protocol):
    """Invokes this Lambda function asynchronously."""

    def invoke(self, payload: Mapping[str, object]) -> None:
        """Queue an asynchronous invocation.

        Args:
            payload: The event to send.
        """


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    """Deployment settings, read from the environment.

    Attributes:
        mail_domain: The mail domain, for example ``ncolony.org``.
        from_address: The address outgoing mail is sent from.
        owner_address: Where synthetic ops messages are addressed.
        configuration_set: The SES configuration set for outgoing mail.
        message_id_domain: The domain SES uses in the Message-IDs it assigns.
        issuer: The Cognito user pool issuer URL.
    """

    mail_domain: str
    from_address: str
    owner_address: str
    configuration_set: str
    message_id_domain: str
    issuer: str


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Context:
    """Everything a flow needs, injected.

    Attributes:
        store: The mail bucket.
        sender: The SES sender.
        invoker: Invokes this function asynchronously.
        settings: Deployment settings.
        clock: Returns the current time in seconds since the epoch.
        new_id: Allocates a new ULID.
        workdir: A writable scratch directory (Lambda's ``/tmp``).
    """

    store: ObjectStore
    sender: Sender
    invoker: Invoker
    settings: Settings
    clock: Callable[[], float]
    new_id: Callable[[], str]
    workdir: str


class ShardSource(Protocol):
    """Provides local copies of index shards."""

    def fetch(self, store: ObjectStore, key: str) -> str:
        """Return a local path to a fresh copy of a shard.

        Args:
            store: The mail bucket.
            key: The shard's key.

        Returns:
            The local path, or an empty string if the shard is gone.
        """
