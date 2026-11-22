"""Test doubles and builders shared by the tests."""

import dataclasses
import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from email.message import EmailMessage

from hamcrest import assert_that, contains_exactly

from .. import ulid
from ..compose import Draft
from ..ingest import ingest
from ..memstore import MemoryStore
from ..model import parse_meta
from ..outbox import enqueue
from ..ports import Context, Envelope, SendRejected, Settings, ShardSource
from ..search import ShardCache
from ..store import ObjectStore

START = 1_767_225_600.0  # 2026-01-01T00:00:00Z
SETTINGS = Settings(
    mail_domain="example.org",
    from_address="Me <me@example.org>",
    owner_address="owner@example.net",
    configuration_set="pidge",
    message_id_domain="us-east-2.amazonses.com",
    issuer="https://cognito-idp.us-east-2.amazonaws.com/us-east-2_TEST",
)


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Clock:
    """A clock that only moves when told to.

    Construct it with :meth:`create`.
    """

    _now: list[float]

    @classmethod
    def create(cls, now: float = START) -> "Clock":
        """Create a clock.

        Args:
            now: The starting time.

        Returns:
            The clock.
        """
        return cls(_now=[now])

    def time(self) -> float:
        """Return the current time.

        Returns:
            Seconds since the epoch.
        """
        return self._now[0]

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Args:
            seconds: How far.
        """
        self._now[0] += seconds


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Ids:
    """Allocates increasing ULIDs at the clock's time.

    Construct it with :meth:`create`.
    """

    _clock: Callable[[], float]
    _counter: list[int]

    @classmethod
    def create(cls, clock: Callable[[], float]) -> "Ids":
        """Create an allocator.

        Args:
            clock: The clock to stamp IDs with.

        Returns:
            The allocator.
        """
        return cls(_clock=clock, _counter=[0])

    def next(self) -> str:
        """Allocate an ID.

        Returns:
            The ULID.
        """
        self._counter[0] += 1
        return ulid.encode(int(self._clock() * 1000), self._counter[0])


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Sender:
    """Records sends; can be told to reject or fail.

    Construct it with :meth:`create`.
    """

    _sent: list[tuple[bytes, Envelope]]
    _failure: list[Exception]

    @classmethod
    def create(cls) -> "Sender":
        """Create a sender that accepts everything.

        Returns:
            The sender.
        """
        return cls(_sent=[], _failure=[])

    def fail_with(self, error: Exception) -> None:
        """Make the next sends raise.

        Args:
            error: The exception to raise.
        """
        self._failure[:] = [error]

    def recover(self) -> None:
        """Make sends succeed again."""
        self._failure.clear()

    def deliveries(self) -> Sequence[tuple[bytes, Envelope]]:
        """Return what was sent.

        Returns:
            Raw messages and envelopes.
        """
        return list(self._sent)

    def send(self, raw: bytes, envelope: Envelope) -> str:
        """Pretend to send.

        Args:
            raw: The MIME.
            envelope: Recipients and tags.

        Returns:
            A fake SES message ID.

        Raises:
            failure: The configured failure, if any.
        """
        for failure in self._failure:
            raise failure
        self._sent.append((raw, envelope))
        return f"ses-{len(self._sent)}"


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Invoker:
    """Records asynchronous invocations.

    Construct it with :meth:`create`.
    """

    _payloads: list[Mapping[str, object]]

    @classmethod
    def create(cls) -> "Invoker":
        """Create an invoker.

        Returns:
            The invoker.
        """
        return cls(_payloads=[])

    def invoke(self, payload: Mapping[str, object]) -> None:
        """Record an invocation.

        Args:
            payload: The event.
        """
        self._payloads.append(payload)

    def invocations(self) -> Sequence[Mapping[str, object]]:
        """Return the recorded invocations.

        Returns:
            The payloads.
        """
        return list(self._payloads)


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class World:
    """A complete fake environment.

    Attributes:
        clock: The clock.
        store: The in-memory bucket.
        sender: The fake SES.
        invoker: The fake Lambda invoker.
        context: The flow context wired to all of the above.
    """

    clock: Clock  # noqa: SLD802
    store: MemoryStore  # noqa: SLD802
    sender: Sender
    invoker: Invoker
    context: Context


def make_world(case: unittest.TestCase) -> World:
    """Build a fake environment for one test.

    Args:
        case: The test case (used to clean up the scratch directory).

    Returns:
        The environment.
    """
    clock = Clock.create()
    store = MemoryStore.create(clock.time)
    sender = Sender.create()
    invoker = Invoker.create()
    scratch = tempfile.TemporaryDirectory()
    case.addCleanup(scratch.cleanup)
    context = Context(
        store=store,
        sender=sender,
        invoker=invoker,
        settings=SETTINGS,
        clock=clock.time,
        new_id=Ids.create(clock.time).next,
        workdir=scratch.name,
    )
    return World(
        clock=clock, store=store, sender=sender, invoker=invoker, context=context
    )


def rejecting() -> Exception:
    """Build the error SES raises in the sandbox.

    Returns:
        The exception.
    """
    return SendRejected("MessageRejected: Email address is not verified.")


def sample_raw(subject: str, body: str, attachments: Mapping[str, bytes]) -> bytes:
    """Build a raw inbound message.

    Args:
        subject: The subject.
        body: The plain-text body.
        attachments: Attachment file names and contents.

    Returns:
        The RFC 5322 bytes.
    """
    message = EmailMessage()
    message["From"] = "Alice Example <alice@example.com>"
    message["To"] = "Me <me@example.org>"
    message["Cc"] = "bob@example.com"
    message["Subject"] = subject
    message["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"
    message["Message-ID"] = "<original@example.com>"
    message["References"] = "<root@example.com>"
    message.set_content(body)
    for name, data in attachments.items():
        message.add_attachment(
            data, maintype="application", subtype="pdf", filename=name
        )
    return message.as_bytes()


def with_store(world: World, store: ObjectStore) -> Context:
    """Return the world's context with a different store.

    Args:
        world: The environment.
        store: The store to use.

    Returns:
        The new context.
    """
    return dataclasses.replace(world.context, store=store)


PLAIN = sample_raw("Hi", "Body", {})


def drop(world: World, name: str, raw: bytes) -> str:
    """Put a raw message in ``inbound/``, as SES would.

    Args:
        world: The environment.
        name: The SES object name.
        raw: The message.

    Returns:
        The object key.
    """
    key = f"inbound/{name}"
    world.store.put(key, raw)
    return key


def deliver(world: World, name: str, raw: bytes) -> str:
    """Receive and ingest a message.

    Args:
        world: The environment.
        name: The SES object name.
        raw: The message.

    Returns:
        The message's ULID.
    """
    return ingest(world.context, drop(world, name, raw))


def post(world: World, subject: str) -> str:
    """Queue a one-line message to ``x@example.com``.

    Args:
        world: The environment.
        subject: The subject.

    Returns:
        The outbox ID.
    """
    return enqueue(
        world.context, Draft(to=["x@example.com"], subject=subject, body=".")
    )


def keys(world: World) -> Sequence[str]:
    """List every stored key.

    Args:
        world: The environment.

    Returns:
        The keys, sorted.
    """
    return world.store.keys()


def body(world: World, key: str) -> bytes:
    """Read a stored object.

    Args:
        world: The environment.
        key: The key.

    Returns:
        The bytes.
    """
    return world.store.get(key).body


def tags_of(world: World, ulid: str) -> Sequence[str]:
    """Read a message's tags.

    Args:
        world: The environment.
        ulid: The message ID.

    Returns:
        The tags.
    """
    return parse_meta(body(world, f"msgs/{ulid}/meta.json")).tags


def new_cache(world: World, name: str) -> ShardSource:
    """Create a shard cache in the world's scratch directory.

    Args:
        world: The environment.
        name: The cache directory name.

    Returns:
        The cache.
    """
    return ShardCache.create(f"{world.context.workdir}/{name}")


def assert_tags(world: World, ulid: str, *expected: str) -> None:
    """Check a message's tags.

    Args:
        world: The environment.
        ulid: The message ID.
        *expected: The tags it should have, in order.
    """
    assert_that(tags_of(world, ulid), contains_exactly(*expected))
