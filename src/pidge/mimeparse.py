"""Parse raw RFC 5322 messages into headers, body text and attachments."""

from __future__ import annotations

import dataclasses
import email
import email.policy
from collections.abc import Sequence
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime

from .htmltext import html_to_text
from .mimeparts import Attachment, part_text, stored_parts


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Headers:
    """The headers that the mailbox indexes and threads on.

    Attributes:
        sender: The From header.
        to: The To addresses.
        cc: The Cc addresses.
        subject: The subject.
        date: The Date header in epoch seconds, or 0 if missing or invalid.
        message_id: The Message-ID header.
        in_reply_to: The In-Reply-To header.
        references: The References header.
    """

    sender: str
    to: Sequence[str]
    cc: Sequence[str]
    subject: str
    date: int
    message_id: str
    in_reply_to: str
    references: str


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class ParsedMessage:
    """A parsed message.

    Attributes:
        headers: The interesting headers.
        body: The body as plain text.
        attachments: The stored parts: the HTML body, if any, then the
            attachments, then the inline parts.
        unresolved_cids: ``cid:`` references in the HTML body that match no
            part.
    """

    headers: Headers
    body: str
    attachments: Sequence[Attachment]
    unresolved_cids: Sequence[str] = ()


def parse_date(value: str) -> int:
    """Parse a Date header.

    Args:
        value: The header value.

    Returns:
        Epoch seconds, or 0 if the value cannot be parsed.
    """
    try:
        return int(parsedate_to_datetime(value).timestamp())
    except (TypeError, ValueError, IndexError):
        return 0


def addresses(message: EmailMessage[str, str], name: str) -> Sequence[str]:
    """Read an address-list header as formatted addresses.

    Args:
        message: The message.
        name: The header name.

    Returns:
        The addresses, as ``Name <addr>`` or bare ``addr``.
    """
    values = [str(value) for value in message.get_all(name, [])]
    return [
        f"{display} <{addr}>" if display else addr
        for display, addr in getaddresses(values)
        if addr
    ]


def header_text(message: EmailMessage[str, str], name: str) -> str:
    """Read a header as stripped text.

    Args:
        message: The message.
        name: The header name.

    Returns:
        The value, or an empty string.
    """
    return str(message.get(name, "")).strip()


def new_message(
    when: float, domain: str, sender: str, recipients: str
) -> EmailMessage[str, str]:
    """Start a new message with From, To, Date and Message-ID headers.

    Args:
        when: The message time, in epoch seconds.
        domain: The domain for the Message-ID.
        sender: The From header.
        recipients: The To header.

    Returns:
        The message.
    """
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipients
    message["Date"] = formatdate(when, usegmt=True)
    message["Message-ID"] = make_msgid(domain=domain)
    return message


def read_headers(message: EmailMessage[str, str]) -> Headers:
    """Extract the indexed headers from a message.

    Args:
        message: The message.

    Returns:
        The headers.
    """
    return Headers(
        sender=str(message.get("From", "")),
        to=addresses(message, "To"),
        cc=addresses(message, "Cc"),
        subject=str(message.get("Subject", "")),
        date=parse_date(str(message.get("Date", ""))),
        message_id=header_text(message, "Message-ID"),
        in_reply_to=header_text(message, "In-Reply-To"),
        references=" ".join(str(message.get("References", "")).split()),
    )


def body_text(message: EmailMessage[str, str]) -> str:
    """Extract the body text, preferring text/plain over HTML.

    Args:
        message: The message.

    Returns:
        The body as plain text, or an empty string if there is none.
    """
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part_text(part)
    if part.get_content_subtype() == "html":
        return html_to_text(content)
    return content


def parse_message(raw: bytes) -> ParsedMessage:
    """Parse a raw message.

    Args:
        raw: The RFC 5322 bytes.

    Returns:
        The parsed message.

    Raises:
        TypeError: If the parser did not produce an EmailMessage.
    """
    message = email.message_from_bytes(raw, policy=email.policy.default)
    if not isinstance(message, EmailMessage):  # pragma: no cover
        raise TypeError("expected an EmailMessage")
    attachments, unresolved = stored_parts(message)
    return ParsedMessage(
        headers=read_headers(message),
        body=body_text(message),
        attachments=attachments,
        unresolved_cids=unresolved,
    )
