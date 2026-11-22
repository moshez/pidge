"""The per-message records under ``msgs/`` and the index markers."""

import dataclasses
from collections.abc import Callable, Iterable, Mapping, Sequence

from . import jsonvalue
from .mimeparse import Headers, ParsedMessage
from .mimeparts import Attachment
from .model import Meta, make_tags, meta_json, parse_meta
from .store import NotFound, ObjectStore, PreconditionFailed

CAS_ATTEMPTS = 20
UNINDEXED = "unindexed/"
DIRTY = "dirty/"


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class NewMessage:
    """Everything needed to create a message's records.

    Attributes:
        raw: The raw RFC 5322 bytes.
        parsed: The parsed message.
        meta: The initial meta record.
        bcc: Blind-copy recipients (outgoing mail only).
    """

    raw: bytes
    parsed: ParsedMessage
    meta: Meta
    bcc: Sequence[str] = ()


def message_key(ulid: str, name: str) -> str:
    """Return the key of one of a message's records.

    Args:
        ulid: The message ID.
        name: The record name, such as ``raw.eml`` or ``meta.json``.

    Returns:
        The S3 key.
    """
    return f"msgs/{ulid}/{name}"


def attachment_key(ulid: str, name: str) -> str:
    """Return the key of a stored attachment.

    Args:
        ulid: The message ID.
        name: The attachment's stored name.

    Returns:
        The S3 key.
    """
    return message_key(ulid, f"attachments/{name}")


def headers_json(headers: Headers, extra: Mapping[str, object]) -> bytes:
    """Serialize the headers record.

    Args:
        headers: The parsed headers.
        extra: Additional members, such as the attachment list.

    Returns:
        The JSON bytes.
    """
    return jsonvalue.dumps(
        {
            "from": headers.sender,
            "to": list(headers.to),
            "cc": list(headers.cc),
            "subject": headers.subject,
            "date": headers.date,
            "message_id": headers.message_id,
            "in_reply_to": headers.in_reply_to,
            "references": headers.references,
            **extra,
        }
    )


def write_attachments(
    store: ObjectStore, ulid: str, attachments: Iterable[Attachment]
) -> Sequence[Mapping[str, object]]:
    """Store a message's attachments.

    Args:
        store: The mail bucket.
        ulid: The message ID.
        attachments: The decoded attachments.

    Returns:
        The attachment list for ``headers.json``.
    """
    listing = []
    for part in attachments:
        store.put(attachment_key(ulid, part.name), part.data)
        entry = dict(
            name=part.name,
            content_type=part.content_type,
            size=len(part.data),
            kind=part.kind.name.lower(),
        )
        listing.append(entry)
    return listing


def write_message(store: ObjectStore, ulid: str, message: NewMessage) -> bool:
    """Create a message's records; safe to repeat.

    Every record is written idempotently. ``meta.json`` is created with
    If-None-Match, so a repeat never overwrites tags changed since.

    Args:
        store: The mail bucket.
        ulid: The message ID.
        message: The message.

    Returns:
        True if this call created the meta record, False if it existed.
    """
    parsed = message.parsed
    listing = write_attachments(store, ulid, parsed.attachments)
    extra = dict(attachments=listing, bcc=list(message.bcc))
    store.put(message_key(ulid, "raw.eml"), message.raw)
    store.put(message_key(ulid, "body.txt"), parsed.body.encode("utf-8"))
    store.put(message_key(ulid, "headers.json"), headers_json(parsed.headers, extra))
    meta = dataclasses.replace(message.meta, unresolved_cids=parsed.unresolved_cids)
    created = True
    try:
        store.put_new(message_key(ulid, "meta.json"), meta_json(meta))
    except PreconditionFailed:
        created = False
    store.put(UNINDEXED + ulid, b"")
    return created


def read_record(store: ObjectStore, ulid: str, name: str) -> bytes:
    """Read one of a message's records.

    Args:
        store: The mail bucket.
        ulid: The message ID.
        name: The record name, such as ``body.txt``.

    Returns:
        The record's bytes.
    """
    return store.get(message_key(ulid, name)).body


def restatus(
    add: Iterable[str], remove: Iterable[str], status: str
) -> Callable[[Meta], Meta]:
    """Build a meta change that adjusts tags and sets the status.

    Args:
        add: Tags to add.
        remove: Tags to remove.
        status: The new status.

    Returns:
        The change function.
    """
    change = retag(add, remove)
    return lambda meta: dataclasses.replace(change(meta), status=status)


def read_meta(store: ObjectStore, ulid: str) -> tuple[Meta, str]:
    """Read a message's meta record.

    Args:
        store: The mail bucket.
        ulid: The message ID.

    Returns:
        The record and its ETag.
    """
    found = store.get(message_key(ulid, "meta.json"))
    return parse_meta(found.body), found.etag


def update_meta(store: ObjectStore, ulid: str, change: Callable[[Meta], Meta]) -> Meta:
    """Apply a change to a message's meta record with compare-and-swap.

    Marks the message dirty so the index picks up the change.

    Args:
        store: The mail bucket.
        ulid: The message ID.
        change: Computes the new record from the current one.

    Returns:
        The new record.

    Raises:
        PreconditionFailed: If every attempt lost a race.
    """
    for _ in range(CAS_ATTEMPTS):
        current, etag = read_meta(store, ulid)
        updated = change(current)
        try:
            store.replace(message_key(ulid, "meta.json"), meta_json(updated), etag)
        except PreconditionFailed:
            continue
        store.put(DIRTY + ulid, b"")
        return updated
    raise PreconditionFailed(ulid)


def retag(add: Iterable[str], remove: Iterable[str]) -> Callable[[Meta], Meta]:
    """Build a meta change that adds and removes tags.

    Args:
        add: Tags to add.
        remove: Tags to remove; removal wins over addition.

    Returns:
        The change function.
    """
    adding, dropping = set(add), set(remove)

    def change(meta: Meta) -> Meta:
        tags = (set(meta.tags) | adding) - dropping
        return dataclasses.replace(meta, tags=make_tags(tags))

    return change


def read_json(store: ObjectStore, key: str) -> Mapping[str, object]:
    """Read a JSON object record, treating a missing one as empty.

    Args:
        store: The mail bucket.
        key: The object key.

    Returns:
        The parsed object, or an empty mapping.
    """
    try:
        return jsonvalue.loads_dict(store.get(key).body)
    except NotFound:
        return {}
