"""Parse raw RFC 5322 messages into headers, body text and attachments."""

from __future__ import annotations

import dataclasses
import email
import email.policy
import html
import html.parser
import re
from collections.abc import Sequence
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime

_SKIPPED_TAGS = frozenset({"script", "style", "head"})
_BLOCK_TAGS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "table"})
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Attachment:
    """A decoded attachment.

    Attributes:
        name: The file name (sanitized for use in an S3 key).
        content_type: The MIME type.
        data: The decoded bytes.
    """

    name: str
    content_type: str
    data: bytes


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
        attachments: The decoded attachments.
    """

    headers: Headers
    body: str
    attachments: Sequence[Attachment]


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class TextExtractor:
    """Collects the visible text of an HTML document.

    Construct it with :meth:`create` and feed it to :func:`html_to_text`.
    """

    _chunks: list[str]
    _skipping: list[str]

    @classmethod
    def create(cls) -> "TextExtractor":
        """Create an empty extractor.

        Returns:
            The extractor.
        """
        return cls(_chunks=[], _skipping=[])

    def start(self, tag: str) -> None:
        """Record an opening tag.

        Args:
            tag: The tag name.
        """
        if tag in _SKIPPED_TAGS:
            self._skipping.append(tag)
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def end(self, tag: str) -> None:
        """Record a closing tag.

        Args:
            tag: The tag name.
        """
        if self._skipping and self._skipping[-1] == tag:
            self._skipping.pop()

    def data(self, text: str) -> None:
        """Record character data.

        Args:
            text: The text.
        """
        if not self._skipping:
            self._chunks.append(text)

    def text(self) -> str:
        """Return the collected text with whitespace tidied.

        Returns:
            The text.
        """
        joined = "".join(self._chunks)
        lines = (" ".join(line.split()) for line in joined.splitlines())
        return "\n".join(line for line in lines if line)


def html_to_text(markup: str) -> str:
    """Convert HTML to readable plain text.

    Args:
        markup: The HTML.

    Returns:
        The visible text.
    """
    extractor = TextExtractor.create()
    parser = html.parser.HTMLParser(convert_charrefs=True)
    setattr(parser, "handle_starttag", lambda tag, attrs: extractor.start(tag))
    setattr(parser, "handle_endtag", extractor.end)
    setattr(parser, "handle_data", extractor.data)
    parser.feed(markup)
    parser.close()
    return html.unescape(extractor.text())


def sanitize_name(name: str) -> str:
    """Make a file name safe to use as part of an S3 key.

    Args:
        name: The original file name.

    Returns:
        A name containing only letters, digits, dots, dashes and underscores.
    """
    cleaned = _UNSAFE_NAME.sub("_", name).strip("._")
    return cleaned[:100] or "attachment"


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
    content = str(part.get_content())
    if part.get_content_subtype() == "html":
        return html_to_text(content)
    return content


def read_attachments(message: EmailMessage[str, str]) -> Sequence[Attachment]:
    """Decode every attachment in a message.

    Args:
        message: The message.

    Returns:
        The attachments, with unique sanitized names.
    """
    found = []
    for index, part in enumerate(message.iter_attachments()):
        payload = part.get_payload(decode=True)
        data = payload if isinstance(payload, bytes) else b""
        name = sanitize_name(part.get_filename() or f"part{index}")
        found.append(
            Attachment(
                name=f"{index}-{name}", content_type=part.get_content_type(), data=data
            )
        )
    return found


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
    return ParsedMessage(
        headers=read_headers(message),
        body=body_text(message),
        attachments=read_attachments(message),
    )
