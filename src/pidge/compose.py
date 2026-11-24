"""Build outgoing MIME messages, including replies and forwarded attachments."""

from __future__ import annotations

import dataclasses
import email.policy
import re
from collections.abc import Mapping, Sequence
from email.message import EmailMessage
from email.utils import getaddresses

from . import jsonvalue
from .mimeparse import new_message
from .ports import Context
from .records import attachment_key, message_key, read_json

_REPLY_PREFIX = re.compile(r"\A\s*re\s*:", re.IGNORECASE)
_ORDINAL = re.compile(r"\A\d+-")


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
class Draft:
    """A message to send.

    Attributes:
        to: Recipient addresses.
        subject: The subject.
        body: The plain-text body.
        cc: Carbon-copy addresses.
        blind: Blind-copy addresses; never written into the MIME.
        in_reply_to: The ULID of the stored message this replies to, or empty.
        attachments: Attachments of stored messages to include.
        sender: The From header, or empty for the configured address.
    """

    to: Sequence[str]
    subject: str
    body: str
    cc: Sequence[str] = ()
    blind: Sequence[str] = ()
    in_reply_to: str = ""
    attachments: Sequence[AttachmentRef] = ()
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
            main, _, sub = kind.partition("/")
            return main, sub or "octet-stream"
    return "application", "octet-stream"


def attach(
    context: Context, message: EmailMessage[str, str], ref: AttachmentRef
) -> None:
    """Copy a stored attachment into a new message.

    Args:
        context: The flow context.
        message: The message being built.
        ref: The attachment.
    """
    data = context.store.get(attachment_key(ref.message, ref.name)).body
    main, sub = content_type_of(context, ref)
    filename = _ORDINAL.sub("", ref.name)
    message.add_attachment(data, maintype=main, subtype=sub, filename=filename)


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
        attach(context, message, ref)
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
