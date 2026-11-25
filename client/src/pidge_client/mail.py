"""Mail to render: messages and search hits, live or from saved JSON."""

import dataclasses
import functools
from collections.abc import Iterable, Mapping, Sequence

from . import jsonshape
from .models import Hit, Message, parse_hit, parse_message


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Mail:
    """The fields the renderers use, from either a message or a search hit.

    Attributes:
        id: The message ID.
        date: The Date header in epoch seconds.
        sender: The From header.
        to: The To addresses.
        subject: The subject, or ``(no subject)``.
        text: The body text, or the snippet for a search hit.
        tags: The tags.
        direction: ``in`` or ``out``.
        status: The delivery status.
        attachments: The names of real attachments (not the body or inline parts).
    """

    id: str
    date: int
    sender: str
    to: Sequence[str]
    subject: str
    text: str
    tags: Sequence[str]
    direction: str
    status: str
    attachments: Sequence[str]


@functools.singledispatch
def as_mail(item: object) -> Mail:
    """Normalize a message or a search hit for rendering.

    Args:
        item: A :class:`Message` or :class:`Hit`.

    Returns:
        The fields to render.

    Raises:
        TypeError: For anything else.
    """
    raise TypeError(f"cannot render {type(item).__name__}")


@as_mail.register
def message_mail(item: Message) -> Mail:
    """Normalize a message.

    Args:
        item: The message.

    Returns:
        The fields to render.
    """
    return Mail(
        id=item.id,
        date=item.headers.date,
        sender=item.headers.sender,
        to=item.headers.to,
        subject=item.headers.subject or "(no subject)",
        text=item.body,
        tags=item.tags,
        direction=item.direction,
        status=item.status,
        attachments=[
            part.name for part in item.headers.attachments if part.kind == "attachment"
        ],
    )


@as_mail.register
def hit_mail(item: Hit) -> Mail:
    """Normalize a search hit.

    Args:
        item: The hit.

    Returns:
        The fields to render.
    """
    return Mail(
        id=item.id,
        date=item.date,
        sender=item.sender,
        to=(),
        subject=item.subject or "(no subject)",
        text=item.snippet,
        tags=item.tags,
        direction="in",
        status="",
        attachments=(),
    )


def load_json(data: object) -> Sequence[Message | Hit]:
    """Read saved tool output: one object, a list, or a search response.

    Args:
        data: The parsed JSON.

    Returns:
        Messages (objects with ``headers``) and search hits (the rest).
    """
    if isinstance(data, Mapping) and "results" in data:
        data = data["results"]
    items = jsonshape.as_list(data) if isinstance(data, list) else [data]
    return [
        parse_message(obj) if "headers" in obj else parse_hit(obj)
        for obj in map(jsonshape.as_dict, items)
    ]


def read_file(path: str) -> str:
    """Read a UTF-8 text file.

    Args:
        path: The path.

    Returns:
        The text.
    """
    with open(path, encoding="utf-8") as stream:
        return stream.read()


def load_files(paths: Iterable[str]) -> Sequence[Message | Hit]:
    """Load saved tool output from JSON files.

    Args:
        paths: The files.

    Returns:
        The messages and hits, in file order.
    """
    return [
        item for path in paths for item in load_json(jsonshape.loads(read_file(path)))
    ]
