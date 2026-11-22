"""The message model: tags, direction and the meta record."""

import dataclasses
import enum
from collections.abc import Iterable, Sequence

from . import jsonvalue


class SystemTag(enum.Enum):
    """Tags that the system itself assigns.

    Attributes:
        INBOX: New inbound mail; archiving removes it.
        SENT: Mail that SES accepted.
        OUTBOX: Mail queued but not yet accepted.
        BOUNCED: Sent mail that bounced.
        REJECTED: Mail that SES refused.
        INGEST_FAILED: Inbound mail that could not be parsed.
        OPS: Synthetic messages about operational problems.
    """

    INBOX = enum.auto()
    SENT = enum.auto()
    OUTBOX = enum.auto()
    BOUNCED = enum.auto()
    REJECTED = enum.auto()
    INGEST_FAILED = enum.auto()
    OPS = enum.auto()


class Direction(enum.Enum):
    """Whether a message was received or sent.

    Attributes:
        IN: Received (including synthetic ops messages).
        OUT: Sent.
    """

    IN = enum.auto()
    OUT = enum.auto()


def tag_name(tag: SystemTag) -> str:
    """Return the string form of a system tag.

    Args:
        tag: The tag.

    Returns:
        For example ``inbox`` or ``ingest-failed``.
    """
    return tag.name.lower().replace("_", "-")


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Meta:
    """The mutable state of a message: the source of truth for tags.

    Attributes:
        tags: The message's tags, sorted.
        direction: Whether it was received or sent.
        status: Free-form delivery status (for sent mail).
        unresolved_cids: ``cid:`` references in the stored HTML body that
            match no part of the message.
    """

    tags: Sequence[str]
    direction: Direction
    status: str
    unresolved_cids: Sequence[str] = ()


def make_tags(tags: Iterable[str]) -> Sequence[str]:
    """Normalize a tag collection.

    Args:
        tags: The tags.

    Returns:
        The distinct tags, sorted.
    """
    return tuple(sorted(set(tags)))


def meta_json(meta: Meta) -> bytes:
    """Serialize a meta record.

    Args:
        meta: The record.

    Returns:
        The JSON bytes.
    """
    record: dict[str, object] = dict(
        tags=list(meta.tags),
        direction=meta.direction.name.lower(),
        status=meta.status,
    )
    if meta.unresolved_cids:
        record.update(unresolved_cids=list(meta.unresolved_cids))
    return jsonvalue.dumps(record)


def parse_meta(data: bytes) -> Meta:
    """Parse a meta record.

    Args:
        data: The JSON bytes.

    Returns:
        The record.
    """
    parsed = jsonvalue.loads_dict(data)
    return Meta(
        tags=make_tags(jsonvalue.string_list(parsed, "tags")),
        direction=Direction[jsonvalue.get_str(parsed, "direction", "in").upper()],
        status=jsonvalue.get_str(parsed, "status"),
        unresolved_cids=tuple(sorted(jsonvalue.string_list(parsed, "unresolved_cids"))),
    )
