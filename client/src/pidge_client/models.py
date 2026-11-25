"""Typed models of what pidge's tools take and return.

The ``parse_*`` functions read the exact JSON a tool returns, so models load
just as well from saved JSON files as from live calls.
"""

import dataclasses
import datetime
from collections.abc import Mapping, Sequence

from . import jsonshape


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Hit:
    """One ``search`` result.

    Attributes:
        id: The message ID (a ULID).
        date: The Date header in epoch seconds.
        sender: The From header.
        subject: The subject.
        snippet: A short excerpt of the body.
        tags: The message's tags.
        ranked: Whether the hit was ranked by relevance.
    """

    id: str
    date: int
    sender: str
    subject: str
    snippet: str
    tags: Sequence[str]
    ranked: bool


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Page[T]:
    """One page of results.

    Attributes:
        hits: The results on this page.
        next_cursor: Pass back as ``cursor`` for the next page; None on the last.
    """

    hits: Sequence[T]
    next_cursor: str | None


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Attachment:
    """A stored part of a message.

    Attributes:
        name: The stored name, such as ``0-body.html`` or ``1-report.pdf``.
        content_type: The MIME type.
        size: The size in bytes.
        kind: ``body`` (the HTML body), ``inline`` or ``attachment``.
    """

    name: str
    content_type: str
    size: int
    kind: str


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Headers:
    """A message's headers and stored parts (``get_message``'s ``headers``).

    Attributes:
        sender: The From header.
        to: The To addresses.
        cc: The Cc addresses.
        blind: The Bcc addresses (sent mail only).
        subject: The subject.
        date: The Date header in epoch seconds.
        message_id: The Message-ID header.
        in_reply_to: The In-Reply-To header.
        references: The References header.
        attachments: The stored parts.
    """

    sender: str
    to: Sequence[str]
    cc: Sequence[str]
    blind: Sequence[str]
    subject: str
    date: int
    message_id: str
    in_reply_to: str
    references: str
    attachments: Sequence[Attachment]


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Message:
    """A ``get_message`` result.

    Attributes:
        id: The message ID (a ULID).
        headers: The headers and stored parts.
        body: The body as plain text.
        tags: The tags.
        status: The delivery status (sent mail).
        direction: ``in`` or ``out``.
    """

    id: str
    headers: Headers
    body: str
    tags: Sequence[str]
    status: str
    direction: str


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Token:
    """A ``get_session_token`` result.

    Attributes:
        token: The bearer token.
        expires_at: When it expires.
        endpoint: The MCP endpoint URL it is good for.
    """

    token: str
    expires_at: datetime.datetime
    endpoint: str


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Queued:
    """A ``send_email`` result.

    Attributes:
        outbox_id: The queued message's ID.
        status: The status (``pending``).
    """

    outbox_id: str
    status: str


def parse_hit(obj: Mapping[str, object]) -> Hit:
    """Parse a search result.

    Args:
        obj: The JSON object.

    Returns:
        The hit.
    """
    return Hit(
        id=jsonshape.get_str(obj, "id"),
        date=jsonshape.get_int(obj, "date"),
        sender=jsonshape.get_str(obj, "from"),
        subject=jsonshape.get_str(obj, "subject"),
        snippet=jsonshape.get_str(obj, "snippet"),
        tags=jsonshape.get_str_list(obj, "tags"),
        ranked=jsonshape.as_bool(obj.get("ranked", False)),
    )


def parse_page(obj: Mapping[str, object]) -> Page[Hit]:
    """Parse a ``search`` response.

    Args:
        obj: The JSON object.

    Returns:
        The page.
    """
    cursor = obj.get("next_cursor")
    return Page(
        hits=tuple(map(parse_hit, jsonshape.get_objects(obj, "results"))),
        next_cursor=None if cursor is None else jsonshape.as_str(cursor),
    )


def parse_attachment(obj: Mapping[str, object]) -> Attachment:
    """Parse an attachment entry.

    Args:
        obj: The JSON object.

    Returns:
        The attachment.
    """
    return Attachment(
        name=jsonshape.get_str(obj, "name"),
        content_type=jsonshape.get_str(obj, "content_type"),
        size=jsonshape.get_int(obj, "size"),
        kind=jsonshape.get_str(obj, "kind", "attachment"),
    )


def parse_headers(obj: Mapping[str, object]) -> Headers:
    """Parse ``get_message``'s ``headers`` object.

    Args:
        obj: The JSON object.

    Returns:
        The headers.
    """
    return Headers(
        sender=jsonshape.get_str(obj, "from"),
        to=jsonshape.get_str_list(obj, "to"),
        cc=jsonshape.get_str_list(obj, "cc"),
        blind=jsonshape.get_str_list(obj, "bcc"),
        subject=jsonshape.get_str(obj, "subject"),
        date=jsonshape.get_int(obj, "date"),
        message_id=jsonshape.get_str(obj, "message_id"),
        in_reply_to=jsonshape.get_str(obj, "in_reply_to"),
        references=jsonshape.get_str(obj, "references"),
        attachments=tuple(
            map(parse_attachment, jsonshape.get_objects(obj, "attachments"))
        ),
    )


def parse_message(obj: Mapping[str, object]) -> Message:
    """Parse a ``get_message`` result.

    Args:
        obj: The JSON object.

    Returns:
        The message.
    """
    return Message(
        id=jsonshape.get_str(obj, "id"),
        headers=parse_headers(jsonshape.as_dict(obj.get("headers", {}))),
        body=jsonshape.get_str(obj, "body"),
        tags=jsonshape.get_str_list(obj, "tags"),
        status=jsonshape.get_str(obj, "status"),
        direction=jsonshape.get_str(obj, "direction", "in"),
    )


def parse_token(obj: Mapping[str, object]) -> Token:
    """Parse a ``get_session_token`` result.

    Args:
        obj: The JSON object.

    Returns:
        The token.
    """
    expires_at = datetime.datetime.fromisoformat(jsonshape.get_str(obj, "expires_at"))
    return Token(
        token=jsonshape.get_str(obj, "token"),
        expires_at=expires_at,
        endpoint=jsonshape.get_str(obj, "endpoint"),
    )


def parse_queued(obj: Mapping[str, object]) -> Queued:
    """Parse a ``send_email`` result.

    Args:
        obj: The JSON object.

    Returns:
        The result.
    """
    return Queued(
        outbox_id=jsonshape.get_str(obj, "outbox_id"),
        status=jsonshape.get_str(obj, "status"),
    )
