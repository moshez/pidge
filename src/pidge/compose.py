"""Build outgoing MIME messages, including replies, forwarded and uploaded files."""

from __future__ import annotations

import dataclasses
import email.policy
import functools
import re
from collections.abc import Mapping, Sequence
from email.message import EmailMessage
from email.utils import getaddresses

from . import jsonvalue
from .mimeparse import new_message
from .ports import Context
from .records import attachment_key, message_key, read_json
from .store import NotFound
from .uploads import read_upload, upload_key

_REPLY_PREFIX = re.compile(r"\A\s*re\s*:", re.IGNORECASE)
_ORDINAL = re.compile(r"\A\d+-")


class MissingAttachment(Exception):
    """An attachment to copy into a new message does not exist."""


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class AttachmentRef:
    """An attachment of a stored message, to include in a new one.

    Attributes:
        message: The ULID of the message that has the attachment.
        name: The attachment's stored name.
    """

    message: str
    name: str


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class UploadRef:
    """A file uploaded through ``put_attachment``, to include in a new message.

    Attributes:
        upload: The upload ID.
    """

    upload: str


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Draft:
    """A message to send.

    Attributes:
        to: Recipient addresses.
        subject: The subject.
        body: The plain-text body.
        cc: Carbon-copy addresses.
        blind: Blind-copy addresses; never written into the MIME.
        in_reply_to: The ULID of the stored message this replies to, or empty.
        attachments: Attachments of stored messages, and uploads, to include.
        sender: The From header, or empty for the configured address.
    """

    to: Sequence[str]
    subject: str
    body: str
    cc: Sequence[str] = ()
    blind: Sequence[str] = ()
    in_reply_to: str = ""
    attachments: Sequence[AttachmentRef | UploadRef] = ()
    sender: str = ""


def recipients(draft: Draft) -> Sequence[str]:
    """List every envelope recipient of a draft.

    Args:
        draft: The draft.

    Returns:
        The bare addresses from To, Cc and Bcc.
    """
    pairs = getaddresses([*draft.to, *draft.cc, *draft.blind])
    return sorted({address for _, address in pairs if address})


def thread_headers(context: Context, draft: Draft) -> Mapping[str, str]:
    """Compute the threading headers for a reply.

    Args:
        context: The flow context.
        draft: The draft.

    Returns:
        ``In-Reply-To``, ``References`` and ``Subject`` values.
    """
    if not draft.in_reply_to:
        return {"Subject": draft.subject}
    original = read_json(context.store, message_key(draft.in_reply_to, "headers.json"))
    parent = jsonvalue.get_str(original, "message_id")
    chain = f"{jsonvalue.get_str(original, 'references')} {parent}".split()
    subject = draft.subject
    if not _REPLY_PREFIX.match(subject):
        subject = f"Re: {subject}"
    return {"In-Reply-To": parent, "References": " ".join(chain), "Subject": subject}


def content_type_of(context: Context, ref: AttachmentRef) -> tuple[str, str]:
    """Look up the MIME type of a stored attachment.

    Args:
        context: The flow context.
        ref: The attachment.

    Returns:
        The main type and subtype.
    """
    headers = read_json(context.store, message_key(ref.message, "headers.json"))
    for entry in jsonvalue.objects(headers, "attachments"):
        if jsonvalue.get_str(entry, "name") == ref.name:
            kind = jsonvalue.get_str(entry, "content_type", "application/octet-stream")
            return split_type(kind)
    return "application", "octet-stream"


def split_type(kind: str) -> tuple[str, str]:
    """Split a MIME type.

    Args:
        kind: The type, such as ``application/pdf``.

    Returns:
        The main type and subtype.
    """
    main, _, sub = kind.partition("/")
    return main, sub or "octet-stream"


@functools.singledispatch
def attach(ref: object, context: Context, message: EmailMessage[str, str]) -> None:
    """Add a file to a new message.

    Args:
        ref: Which file.
        context: The flow context.
        message: The message being built.

    Raises:
        TypeError: For an unknown kind of reference.
    """
    raise TypeError(f"cannot attach {ref!r}")


@attach.register(AttachmentRef)
def attach_stored(
    ref: AttachmentRef, context: Context, message: EmailMessage[str, str]
) -> None:
    """Copy a stored attachment into a new message.

    Args:
        ref: The attachment.
        context: The flow context.
        message: The message being built.

    Raises:
        MissingAttachment: If the message has no such attachment.
    """
    try:
        data = context.store.get(attachment_key(ref.message, ref.name)).body
    except NotFound:
        raise MissingAttachment(
            f"no attachment {ref.name!r} on {ref.message}"
        ) from None
    main, sub = content_type_of(context, ref)
    filename = _ORDINAL.sub("", ref.name)
    message.add_attachment(data, maintype=main, subtype=sub, filename=filename)


@attach.register(UploadRef)
def attach_upload(
    ref: UploadRef, context: Context, message: EmailMessage[str, str]
) -> None:
    """Add an uploaded file to a new message.

    Args:
        ref: The upload.
        context: The flow context.
        message: The message being built.

    Raises:
        MissingAttachment: If there is no such upload, or its bytes have
            not been PUT yet.
    """
    try:
        upload = read_upload(context.store, ref.upload)
    except NotFound:
        raise MissingAttachment(f"no upload {ref.upload}") from None
    try:
        data = context.store.get(upload_key(ref.upload, "data")).body
    except NotFound:
        raise MissingAttachment(
            f"upload {ref.upload} has no data: PUT the file to its URL first"
        ) from None
    main, sub = split_type(upload.content_type)
    message.add_attachment(data, maintype=main, subtype=sub, filename=upload.name)


def compose(context: Context, draft: Draft) -> bytes:
    """Build the MIME for a draft.

    Args:
        context: The flow context.
        draft: The draft.

    Returns:
        The raw message, with CRLF line endings.
    """
    settings = context.settings
    message = new_message(
        context.clock(),
        settings.mail_domain,
        draft.sender or settings.from_address,
        ", ".join(draft.to),
    )
    if draft.cc:
        message["Cc"] = ", ".join(draft.cc)
    for name, value in thread_headers(context, draft).items():
        message[name] = value
    message.set_content(draft.body)
    for ref in draft.attachments:
        attach(ref, context, message)
    return message.as_bytes(policy=email.policy.SMTP)


def replace_message_id(raw: bytes, message_id: str) -> bytes:
    """Rewrite the Message-ID header of a raw message.

    Args:
        raw: The message.
        message_id: The new value, including angle brackets.

    Returns:
        The message with the header replaced (or added).
    """
    head, separator, body = raw.partition(b"\r\n\r\n")
    lines = [
        line
        for line in head.split(b"\r\n")
        if not line.lower().startswith(b"message-id:")
    ]
    lines.append(b"Message-ID: " + message_id.encode("ascii"))
    return b"\r\n".join(lines) + separator + body
