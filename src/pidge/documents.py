"""Messages as the index sees them, and the query language over them."""

import dataclasses
import re
from collections.abc import Mapping, Sequence

from . import jsonvalue, ulid
from .model import Meta
from .ordering import Order, Position

_TERM = re.compile(r"[^\W_]+")
SNIPPET_CHARS = 160


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class IndexDoc:
    """A message as the index sees it.

    Attributes:
        ulid: The message ID.
        headers: The parsed ``headers.json`` record.
        body: The body text.
        meta: The meta record.
    """

    ulid: str
    headers: Mapping[str, object]
    body: str
    meta: Meta


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Query:
    """A search request; the same language for the index and the tail.

    Attributes:
        terms: Lower-case words that must all appear.
        sender: Substring of the From header.
        recipient: Substring of the To or Cc headers.
        after: Only messages on or after this epoch second.
        before: Only messages before this epoch second (0 for no limit).
        tags: Tags that must all be present.
        has_attachments: Filter on attachments; None for either.
        limit: The maximum number of results.
        order: How to list the results.
        cursor: Only messages whose ``(date, ulid)`` sorts before this
            position; None for no cursor.
    """

    terms: Sequence[str] = ()
    sender: str = ""
    recipient: str = ""
    after: int = 0
    before: int = 0
    tags: Sequence[str] = ()
    has_attachments: bool | None = None
    limit: int = 20
    order: Order = Order.RELEVANCE
    cursor: Position | None = None


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Hit:
    """One search result.

    Attributes:
        ulid: The message ID.
        date: The message date in epoch seconds.
        sender: The From header.
        subject: The subject.
        snippet: A short excerpt of the body.
        tags: The message's tags.
        score: The BM25 score (lower is better); None for unranked hits.
    """

    ulid: str
    date: int
    sender: str
    subject: str
    snippet: str
    tags: Sequence[str]
    score: float | None


def words(text: str) -> Sequence[str]:
    """Split text into lower-case words, the way FTS5's unicode61 does.

    Args:
        text: The text.

    Returns:
        The words.
    """
    return [word.lower() for word in _TERM.findall(text)]


def doc_date(doc: IndexDoc) -> int:
    """Return the date to sort and filter a message by.

    Args:
        doc: The message.

    Returns:
        The Date header, or the ULID's timestamp if the header was missing.
    """
    date = int(jsonvalue.get_number(doc.headers, "date"))
    return date or int(ulid.timestamp(doc.ulid))


def addresses_text(doc: IndexDoc, name: str) -> str:
    """Join an address-list header into one string.

    Args:
        doc: The message.
        name: ``to``, ``cc`` or ``bcc``.

    Returns:
        The addresses separated by commas.
    """
    return ", ".join(jsonvalue.string_list(doc.headers, name))


def attachment_names(doc: IndexDoc) -> Sequence[str]:
    """List a message's real attachment names.

    The stored HTML body and inline parts are listed in ``headers.json`` too,
    but are left out here, so they neither set ``has_attachments`` nor become
    searchable words.

    Args:
        doc: The message.

    Returns:
        The stored attachment names.
    """
    return [
        jsonvalue.get_str(entry, "name")
        for entry in jsonvalue.objects(doc.headers, "attachments")
        if jsonvalue.get_str(entry, "kind", "attachment") == "attachment"
    ]


def searchable_text(doc: IndexDoc) -> Sequence[str]:
    """List the words the index would have for a message.

    Args:
        doc: The message.

    Returns:
        The words.
    """
    parts = [
        jsonvalue.get_str(doc.headers, "subject"),
        jsonvalue.get_str(doc.headers, "from"),
        addresses_text(doc, "to"),
        addresses_text(doc, "cc"),
        doc.body,
        " ".join(attachment_names(doc)),
    ]
    return words(" ".join(parts))


def before_position(date: int, doc_id: str, cursor: Position | None) -> bool:
    """Check whether a message comes after a cursor in date order.

    Args:
        date: The message's date.
        doc_id: The message's ID.
        cursor: The cursor, or None.

    Returns:
        True if there is no cursor or the message sorts before it.
    """
    return cursor is None or (date, doc_id) < (cursor.date, cursor.ulid)


def matches(doc: IndexDoc, query: Query) -> bool:
    """Check a tail message against a query, the way the index would.

    Args:
        doc: The message.
        query: The query.

    Returns:
        True if it matches.
    """
    headers = doc.headers
    date = doc_date(doc)
    recipients = f"{addresses_text(doc, 'to')} {addresses_text(doc, 'cc')}"
    checks = [
        set(query.terms) <= set(searchable_text(doc)),
        query.sender.lower() in jsonvalue.get_str(headers, "from").lower(),
        query.recipient.lower() in recipients.lower(),
        date >= query.after,
        not query.before or date < query.before,
        set(query.tags) <= set(doc.meta.tags),
        query.has_attachments in (None, bool(attachment_names(doc))),
        before_position(date, doc.ulid, query.cursor),
    ]
    return all(checks)


def tail_hit(doc: IndexDoc) -> Hit:
    """Describe a tail message as an unranked hit.

    Args:
        doc: The message.

    Returns:
        The hit.
    """
    return Hit(
        ulid=doc.ulid,
        date=doc_date(doc),
        sender=jsonvalue.get_str(doc.headers, "from"),
        subject=jsonvalue.get_str(doc.headers, "subject"),
        snippet=doc.body[:SNIPPET_CHARS],
        tags=doc.meta.tags,
        score=None,
    )
