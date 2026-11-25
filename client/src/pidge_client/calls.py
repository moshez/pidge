"""Typed wrappers, one per pidge tool.

Each tool's parameters are declared once, in a :class:`Signature`, with
their JSON types (``string``, ``integer``, ``boolean``, ``string[]``,
``object[]``). The wrappers build their arguments through it, and the tests
compare every signature with the server's ``tools/list``, so the client and
the server cannot drift apart unnoticed.
"""

import dataclasses
import functools
from collections.abc import Mapping, Sequence

from . import jsonshape, models
from .models import Hit, Message, Page, Queued, Token
from .transport import Session, call_tool

# Request fields named differently from the tool parameter (``from`` is a keyword).
RENAMED = {"sender": "from", "blind": "bcc"}


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Signature:
    """A tool's name and parameters.

    Attributes:
        tool: The tool name.
        params: Each parameter's name and JSON type.
    """

    tool: str
    params: Mapping[str, str]


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class SearchArgs:
    """``search`` arguments; None leaves one out.

    Attributes:
        query: Words that must all match.
        sender: Filter on the From header (the tool's ``from``).
        to: Filter on recipients.
        after: Only mail dated after this ISO 8601 date.
        before: Only mail dated before this ISO 8601 date.
        tags: Only mail with all of these tags.
        has_attachments: Only mail with (or without) attachments.
        limit: Results per page (1 to 100).
        order: ``"date"`` or ``"relevance"``.
        cursor: ``next_cursor`` of the previous date-order page.
    """

    query: str | None = None
    sender: str | None = None
    to: str | None = None
    after: str | None = None
    before: str | None = None
    tags: Sequence[str] | None = None
    has_attachments: bool | None = None
    limit: int | None = None
    order: str | None = None
    cursor: str | None = None


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class AttachFrom:
    """An attachment of a stored message, to attach to an outgoing one.

    Attributes:
        id: The stored message's ID.
        name: The attachment's stored name.
    """

    id: str
    name: str


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Draft:
    """``send_email`` arguments.

    Attributes:
        to: The recipients.
        subject: The subject.
        body: The plain-text body.
        cc: Cc recipients.
        blind: Bcc recipients (the tool's ``bcc``).
        in_reply_to_id: The ID of the message this replies to.
        sender: The From address, under the mail domain (the tool's ``from``).
        attach_from: Attachments to copy from stored messages.
    """

    to: Sequence[str]
    subject: str
    body: str
    cc: Sequence[str] | None = None
    blind: Sequence[str] | None = None
    in_reply_to_id: str | None = None
    sender: str | None = None
    attach_from: Sequence[AttachFrom] | None = None


SEARCH = Signature(
    tool="search",
    params={
        "query": "string",
        "from": "string",
        "to": "string",
        "after": "string",
        "before": "string",
        "tags": "string[]",
        "has_attachments": "boolean",
        "limit": "integer",
        "order": "string",
        "cursor": "string",
    },
)
GET_MESSAGE = Signature(tool="get_message", params={"id": "string"})
GET_ATTACHMENT = Signature(
    tool="get_attachment", params={"id": "string", "name": "string"}
)
LIST_TAGS = Signature(tool="list_tags", params={})
TAG_EMAIL = Signature(
    tool="tag_email", params={"id": "string", "add": "string[]", "remove": "string[]"}
)
SEND_EMAIL = Signature(
    tool="send_email",
    params={
        "to": "string[]",
        "cc": "string[]",
        "bcc": "string[]",
        "subject": "string",
        "body": "string",
        "in_reply_to_id": "string",
        "from": "string",
        "attach_from": "object[]",
    },
)
GET_SESSION_TOKEN = Signature(tool="get_session_token", params={})
REFRESH_TOOLS = Signature(tool="refresh_tools", params={})
SIGNATURES = (
    SEARCH,
    GET_MESSAGE,
    GET_ATTACHMENT,
    LIST_TAGS,
    TAG_EMAIL,
    SEND_EMAIL,
    GET_SESSION_TOKEN,
    REFRESH_TOOLS,
)


@functools.singledispatch
def to_json(value: object) -> object:
    """Convert an argument value to JSON (scalars pass through).

    Args:
        value: The value.

    Returns:
        The JSON value.
    """
    return value


@to_json.register
def attachment_json(value: AttachFrom) -> object:
    """Convert an attachment reference.

    Args:
        value: The reference.

    Returns:
        ``{"id": ..., "name": ...}``.
    """
    return dict(id=value.id, name=value.name)


@to_json.register(list)
@to_json.register(tuple)
def sequence_json(value: Sequence[object]) -> object:
    """Convert a list of values.

    Args:
        value: The values.

    Returns:
        A JSON array.
    """
    return [to_json(item) for item in value]


def call(
    session: Session, signature: Signature, values: Mapping[str, object]
) -> Mapping[str, object]:
    """Call a tool, leaving out unset (None) values.

    Args:
        session: The session.
        signature: The tool.
        values: Parameter values by name.

    Returns:
        The tool's structured result.

    Raises:
        KeyError: If a name is not one of the tool's parameters.
    """
    unknown = sorted(set(values) - set(signature.params))
    if unknown:
        raise KeyError(f"{signature.tool} has no parameters {unknown}")
    arguments = {
        name: to_json(value) for name, value in values.items() if value is not None
    }
    return call_tool(session, signature.tool, arguments)


def request_values(request: SearchArgs | Draft) -> Mapping[str, object]:
    """Read a request's fields under their tool parameter names.

    Args:
        request: The request.

    Returns:
        Values by parameter name.
    """
    return {
        RENAMED.get(field.name, field.name): getattr(request, field.name)
        for field in dataclasses.fields(request)
    }


def search(session: Session, args: SearchArgs = SearchArgs()) -> Page[Hit]:
    """SearchArgs the mailbox.

    Args:
        session: The session.
        args: The search arguments.

    Returns:
        One page of hits.
    """
    return models.parse_page(call(session, SEARCH, request_values(args)))


def get_message(session: Session, ulid: str) -> Message:
    """Read a message.

    Args:
        session: The session.
        ulid: The message ID.

    Returns:
        The message.
    """
    return models.parse_message(call(session, GET_MESSAGE, {"id": ulid}))


def get_attachment(session: Session, ulid: str, name: str) -> str:
    """Get a short-lived download URL for a stored part.

    Args:
        session: The session.
        ulid: The message ID.
        name: The attachment's stored name (for example ``0-body.html``).

    Returns:
        The presigned URL.
    """
    found = call(session, GET_ATTACHMENT, {"id": ulid, "name": name})
    return jsonshape.get_str(found, "url")


def list_tags(session: Session) -> Mapping[str, int]:
    """Count messages per tag.

    Args:
        session: The session.

    Returns:
        Message counts by tag.
    """
    counts = jsonshape.as_dict(call(session, LIST_TAGS, {}).get("tags", {}))
    return {tag: jsonshape.as_int(count) for tag, count in counts.items()}


def tag_email(
    session: Session, ulid: str, add: Sequence[str] = (), remove: Sequence[str] = ()
) -> Sequence[str]:
    """Add and remove tags.

    Args:
        session: The session.
        ulid: The message ID.
        add: Tags to add.
        remove: Tags to remove.

    Returns:
        The message's tags afterwards.
    """
    found = call(session, TAG_EMAIL, {"id": ulid, "add": add, "remove": remove})
    return jsonshape.get_str_list(found, "tags")


def send_email(session: Session, draft: Draft) -> Queued:
    """Queue an email for sending.

    Args:
        session: The session.
        draft: The message.

    Returns:
        The outbox ID and status.
    """
    return models.parse_queued(call(session, SEND_EMAIL, request_values(draft)))


def get_session_token(session: Session) -> Token:
    """Get a fresh 5-minute bearer token for this endpoint.

    Args:
        session: The session.

    Returns:
        The token.
    """
    return models.parse_token(call(session, GET_SESSION_TOKEN, {}))


def refresh_tools(session: Session) -> models.Refreshed:
    """List every tool the server has, and its build.

    The server sends ``notifications/tools/list_changed`` along with the
    result (over SSE); the client ignores it.

    Args:
        session: The session.

    Returns:
        The tools (name and one-sentence summary) and the build.
    """
    return models.parse_refreshed(call(session, REFRESH_TOOLS, {}))
