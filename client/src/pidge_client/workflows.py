"""Multi-call workflows built on the typed calls."""

import dataclasses
from collections.abc import Iterable, Iterator, Mapping, Sequence

from . import calls
from .calls import SearchArgs
from .models import Hit, Message
from .transport import Session, check_status

BODY_KIND = "body"
PAGE_SIZE = 100


def iter_search(session: Session, args: SearchArgs = SearchArgs()) -> Iterator[Hit]:
    """Yield every matching message, newest first, following ``next_cursor``.

    This always searches in date order, since only date order pages; query
    words are a filter. ``args.limit`` is the page size (default 100).

    Args:
        session: The session.
        args: The search; its ``order`` and ``cursor`` are ignored.

    Yields:
        The hits.
    """
    page_args = dataclasses.replace(
        args, order="date", cursor=None, limit=args.limit or PAGE_SIZE
    )
    while True:
        page = calls.search(session, page_args)
        yield from page.hits
        if page.next_cursor is None:
            return
        page_args = dataclasses.replace(page_args, cursor=page.next_cursor)


def body_part(message: Message) -> str | None:
    """Find the stored name of a message's HTML body.

    Args:
        message: The message.

    Returns:
        The name (normally ``0-body.html``), or None for plain-text mail.
    """
    for attachment in message.headers.attachments:
        if attachment.kind == BODY_KIND:
            return attachment.name
    return None


def fetch_body_html(session: Session, message: Message) -> str | None:
    """Download a message's original HTML body.

    The presigned URL is fetched with a plain GET and no Authorization
    header (S3 rejects requests that carry both).

    Args:
        session: The session.
        message: The message.

    Returns:
        The HTML, or None if the message has no HTML body. A failed
        download raises HTTPStatusError.
    """
    name = body_part(message)
    if name is None:
        return None
    reply = session.transport.get(calls.get_attachment(session, message.id, name))
    check_status(reply, 200)
    return reply.body.decode("utf-8", "replace")


def upload_attachment(
    session: Session, name: str, data: bytes, content_type: str | None = None
) -> str:
    """Upload a new file for ``send_email`` to attach.

    Calls ``put_attachment``, then PUTs the bytes to the presigned URL with
    no Authorization header.

    Args:
        session: The session.
        name: The attachment's file name.
        data: The file.
        content_type: Its MIME type (server default application/octet-stream).

    Returns:
        The upload ID, for ``AttachUpload(upload=...)`` in a draft. A failed
        upload raises HTTPStatusError.
    """
    upload = calls.put_attachment(session, name, len(data), content_type)
    check_status(session.transport.put(upload.url, data), 200)
    return upload.upload_id


def archive(session: Session, ids: Iterable[str]) -> Mapping[str, Sequence[str]]:
    """Archive messages: remove the ``inbox`` tag from each.

    Args:
        session: The session.
        ids: The message IDs.

    Returns:
        Each message's tags afterwards.
    """
    return {ulid: calls.tag_email(session, ulid, remove=["inbox"]) for ulid in ids}
