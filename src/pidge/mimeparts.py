"""Choose and decode the parts of a message stored under ``attachments/``.

The stored parts are the HTML body (``0-body.html``), then the attachments,
numbered from 1, then every other part with a Content-ID, so that the HTML
renders from the files stored beside it: each ``cid:`` reference in the
HTML is rewritten to the bare name of the stored part.
"""

from __future__ import annotations

import dataclasses
import enum
import mimetypes
import re
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from email.message import EmailMessage, MIMEPart

BODY_HTML = "0-body.html"
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
# Only the built-in table, so names do not depend on the host's mime.types.
_TYPES = mimetypes.MimeTypes()
_ATTRIBUTE = re.compile(
    r"""(?P<lead>\b(?:src|href|background)\s*=\s*["']?)cid:(?P<cid>[^"'\s>]+)""",
    re.IGNORECASE,
)
_CSS_URL = re.compile(
    r"""(?P<lead>\burl\(\s*["']?)cid:(?P<cid>[^"')\s]+)""", re.IGNORECASE
)
_META_CHARSET = re.compile(
    r"""(?P<lead><meta\b[^>]*?\bcharset\s*=\s*["']?)[\w.:-]+""", re.IGNORECASE
)


def content_id_key(value: str) -> str:
    """Normalize a Content-ID, or a ``cid:`` reference, for matching.

    Args:
        value: The header value or the reference (without ``cid:``).

    Returns:
        The value without whitespace or angle brackets, in lower case.
    """
    return value.strip().strip("<>").strip().lower()


def inline_name(content_type: str, number: int) -> str:
    """Name an inline part that has no file name.

    Args:
        content_type: The part's MIME type.
        number: Counts the unnamed inline parts, from 1.

    Returns:
        For example ``inline-1.png``.
    """
    extension = _TYPES.guess_extension(content_type) or ".bin"
    return f"inline-{number}{extension}"


def declare_utf8(markup: str) -> str:
    """Make any ``<meta>`` charset declaration say UTF-8, as stored.

    Args:
        markup: The HTML, already decoded.

    Returns:
        The HTML with its declared charset replaced.
    """
    return _META_CHARSET.sub(lambda match: match["lead"] + "utf-8", markup)


def rewrite_cids(markup: str, names: Mapping[str, str]) -> tuple[str, Sequence[str]]:
    """Point every resolvable ``cid:`` reference at its stored file.

    References in ``src``, ``href`` and ``background`` attributes and in CSS
    ``url()`` (inline styles and ``<style>`` blocks) are URL-decoded and
    matched case-insensitively.

    Args:
        markup: The HTML.
        names: Stored file names, by :func:`content_id_key`.

    Returns:
        The rewritten HTML, and the references that matched no part (left
        unchanged), sorted and without duplicates.
    """
    unresolved: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        reference = urllib.parse.unquote(match["cid"])
        name = names.get(content_id_key(reference))
        if name is None:
            unresolved.add(reference)
            return match[0]
        return match["lead"] + name

    for pattern in (_ATTRIBUTE, _CSS_URL):
        markup = pattern.sub(replace, markup)
    return markup, sorted(unresolved)


class PartKind(enum.Enum):
    """Why a part is stored under ``attachments/``.

    Attributes:
        ATTACHMENT: A real attachment; counts for ``has_attachments``.
        BODY: The HTML body, stored as ``0-body.html``.
        INLINE: An inline part (it has a Content-ID) of the body.
    """

    ATTACHMENT = enum.auto()
    BODY = enum.auto()
    INLINE = enum.auto()


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Attachment:
    """A decoded part, stored under ``attachments/``.

    Attributes:
        name: The file name (sanitized for use in an S3 key).
        content_type: The MIME type.
        data: The decoded bytes.
        kind: Why it is stored.
    """

    name: str
    content_type: str
    data: bytes
    kind: PartKind = PartKind.ATTACHMENT


def sanitize_name(name: str) -> str:
    """Make a file name safe to use as part of an S3 key.

    Args:
        name: The original file name.

    Returns:
        A name containing only letters, digits, dots, dashes and underscores.
    """
    cleaned = _UNSAFE_NAME.sub("_", name).strip("._")
    return cleaned[:100] or "attachment"


def part_text(part: MIMEPart[str, str]) -> str:
    """Decode a text part, even when it names a charset Python lacks.

    Args:
        part: The text part.

    Returns:
        The text; an unknown charset is decoded as UTF-8 with replacements.
    """
    try:
        return str(part.get_content())
    except LookupError:
        payload = part.get_payload(decode=True)
        data = payload if isinstance(payload, bytes) else b""
        return data.decode("utf-8", errors="replace")


def payload_bytes(part: MIMEPart[str, str]) -> bytes:
    """Decode a part's transfer encoding.

    Args:
        part: The part.

    Returns:
        The bytes (empty for a multipart).
    """
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def content_id(part: MIMEPart[str, str]) -> str:
    """Read a part's Content-ID for matching.

    Args:
        part: The part.

    Returns:
        The normalized Content-ID, or an empty string.
    """
    return content_id_key(str(part.get("Content-ID", "")))


def leaf_parts(part: MIMEPart[str, str]) -> Iterator[MIMEPart[str, str]]:
    """Walk the non-multipart parts, without entering attached messages.

    Args:
        part: Where to start.

    Yields:
        The leaf parts, in order.
    """
    if part.get_content_maintype() == "multipart":
        for child in part.iter_parts():
            yield from leaf_parts(child)
    else:
        yield part


def stored_names(chosen: Sequence[MIMEPart[str, str]]) -> Sequence[str]:
    """Name the stored parts ``{n}-{name}``, numbered from 1.

    Args:
        chosen: The parts to store.

    Returns:
        One name per part: its file name, ``inline-{k}.{ext}`` for a part
        with a Content-ID and no file name, or else ``part{n}``.
    """
    names = []
    unnamed = 0
    for number, part in enumerate(chosen, start=1):
        filename = part.get_filename()
        if filename:
            base = sanitize_name(filename)
        elif content_id(part):
            unnamed += 1
            base = inline_name(part.get_content_type(), unnamed)
        else:
            base = f"part{number}"
        names.append(f"{number}-{base}")
    return names


def inline_parts(
    message: EmailMessage[str, str],
    body: MIMEPart[str, str] | None,
    attached: Sequence[MIMEPart[str, str]],
) -> Sequence[MIMEPart[str, str]]:
    """Find the parts with a Content-ID that would not be stored otherwise.

    Args:
        message: The message.
        body: The HTML body part, which is stored separately.
        attached: The parts ``iter_attachments`` yields.

    Returns:
        The other leaf parts that have a Content-ID, in order.
    """
    stored = [id(part) for part in attached]
    if body is not None:
        stored.append(id(body))
    return [
        part
        for part in leaf_parts(message)
        if content_id(part) and id(part) not in stored
    ]


def decode_part(part: MIMEPart[str, str], name: str, kind: PartKind) -> Attachment:
    """Decode a part for storage.

    Args:
        part: The part.
        name: Its stored name.
        kind: Why it is stored.

    Returns:
        The decoded part.
    """
    return Attachment(
        name=name,
        content_type=part.get_content_type(),
        data=payload_bytes(part),
        kind=kind,
    )


def read_attachments(
    message: EmailMessage[str, str], body: MIMEPart[str, str] | None
) -> Sequence[tuple[MIMEPart[str, str], Attachment]]:
    """Decode the attachments, then every other part with a Content-ID.

    Args:
        message: The message.
        body: The HTML body part, which is stored separately.

    Returns:
        Each part with its decoded form, in a fixed order, with unique names.
    """
    attached = list(message.iter_attachments())
    inline = inline_parts(message, body, attached)
    kinds = [PartKind.ATTACHMENT] * len(attached) + [PartKind.INLINE] * len(inline)
    chosen = [*attached, *inline]
    labels = zip(chosen, stored_names(chosen), kinds, strict=True)
    return [(part, decode_part(part, name, kind)) for part, name, kind in labels]


def html_file(
    body: MIMEPart[str, str], names: Mapping[str, str]
) -> tuple[Attachment, Sequence[str]]:
    """Store the HTML body as UTF-8, its ``cid:`` references made local.

    Args:
        body: The HTML body part.
        names: Stored file names, by normalized Content-ID.

    Returns:
        The ``0-body.html`` part and the unresolved references.
    """
    markup, unresolved = rewrite_cids(declare_utf8(part_text(body)), names)
    stored = Attachment(
        name=BODY_HTML,
        content_type="text/html",
        data=markup.encode("utf-8"),
        kind=PartKind.BODY,
    )
    return stored, unresolved


def stored_parts(
    message: EmailMessage[str, str],
) -> tuple[Sequence[Attachment], Sequence[str]]:
    """Decode every part stored under ``attachments/``.

    Args:
        message: The message.

    Returns:
        The HTML body (if any), the attachments and the inline parts; and
        the ``cid:`` references in the HTML that match no part.
    """
    body = message.get_body(preferencelist=("html",))
    pairs = read_attachments(message, body)
    stored = [decoded for _, decoded in pairs]
    if body is None:
        return stored, ()
    names = {
        content_id(part): decoded.name
        for part, decoded in reversed(pairs)
        if content_id(part)
    }
    page, unresolved = html_file(body, names)
    return [page, *stored], unresolved
