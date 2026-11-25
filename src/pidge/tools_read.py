"""The MCP tools that read the mailbox."""

from collections.abc import Mapping

from . import arguments, jsonvalue
from .documents import Hit, Query, words
from .ordering import Order, Position
from .records import attachment_key, read_meta, read_record
from .search import Page, search
from .store import NotFound, ObjectStore
from .tagging import list_tags
from .toolkit import MESSAGE_ID, TEXT, TEXT_LIST, Services, Tool, ToolError, schema

MAX_RESULTS = 100
ORDERS = {order.name.lower(): order for order in Order}


def hit_json(hit: Hit) -> Mapping[str, object]:
    """Describe a search hit.

    Args:
        hit: The hit.

    Returns:
        The JSON-ready description.
    """
    return {
        "id": hit.ulid,
        "date": hit.date,
        "from": hit.sender,
        "subject": hit.subject,
        "snippet": hit.snippet,
        "tags": list(hit.tags),
        "ranked": hit.score is not None,
    }


def search_order(
    args: Mapping[str, object], default: Order
) -> tuple[Order, Position | None]:
    """Read the ``search`` tool's order and cursor.

    Args:
        args: The tool arguments.
        default: The order when none is given.

    Returns:
        The order, and the cursor if one was given.

    Raises:
        ToolError: If a cursor is given for relevance order.
    """
    order = arguments.choice(args, "order", ORDERS, default)
    cursor = arguments.cursor(args, "cursor")
    if cursor is not None and order is not Order.DATE:
        raise ToolError("cursor only works with order 'date'")
    return order, cursor


def search_query(args: Mapping[str, object]) -> Query:
    """Build a query from the ``search`` tool's arguments.

    Args:
        args: The tool arguments.

    Returns:
        The query.
    """
    terms = words(arguments.text(args, "query"))
    order, cursor = search_order(args, Order.RELEVANCE if terms else Order.DATE)
    return Query(
        terms=terms,
        sender=arguments.text(args, "from"),
        recipient=arguments.text(args, "to"),
        after=arguments.date(args, "after"),
        before=arguments.date(args, "before"),
        tags=arguments.strings(args, "tags"),
        has_attachments=arguments.flag(args, "has_attachments"),
        limit=arguments.bounded(args, "limit", MAX_RESULTS),
        order=order,
        cursor=cursor,
    )


def page_json(query: Query, page: Page) -> Mapping[str, object]:
    """Describe a page of results.

    Args:
        query: The query.
        page: The page.

    Returns:
        The hits, and a cursor for the next page in date order (else null).
    """
    next_cursor = None
    if query.order is Order.DATE and page.more:
        last = page.hits[-1]
        next_cursor = arguments.encode_cursor(Position(date=last.date, ulid=last.ulid))
    return dict(results=[hit_json(hit) for hit in page.hits], next_cursor=next_cursor)


def run_search(services: Services, args: Mapping[str, object]) -> object:
    """Search messages.

    Args:
        services: The services.
        args: The tool arguments.

    Returns:
        The results, and a cursor for the next page in date order.
    """
    query = search_query(args)
    return page_json(query, search(services.context.store, services.cache, query))


def read_message(store: ObjectStore, ulid: str) -> Mapping[str, object]:
    """Read a stored message's records.

    Args:
        store: The mail bucket.
        ulid: The message ID.

    Returns:
        Headers, body, tags, status and direction.
    """
    meta, _ = read_meta(store, ulid)
    body = read_record(store, ulid, "body.txt")
    return dict(
        id=ulid,
        headers=jsonvalue.loads_dict(read_record(store, ulid, "headers.json")),
        body=body.decode("utf-8", "replace"),
        tags=list(meta.tags),
        status=meta.status,
        direction=meta.direction.name.lower(),
    )


def run_get_message(services: Services, args: Mapping[str, object]) -> object:
    """Read a message.

    Args:
        services: The services.
        args: The tool arguments.

    Returns:
        Headers, body, attachments and tags.

    Raises:
        ToolError: If there is no such message.
    """
    ulid = arguments.message_id(args, "id")
    try:
        return read_message(services.context.store, ulid)
    except NotFound:
        raise ToolError(f"no message {ulid}") from None


def run_get_attachment(services: Services, args: Mapping[str, object]) -> object:
    """Create a download link for an attachment.

    Args:
        services: The services.
        args: The tool arguments.

    Returns:
        The presigned URL.

    Raises:
        ToolError: If there is no such attachment.
    """
    ulid = arguments.message_id(args, "id")
    name = arguments.required_text(args, "name")
    store = services.context.store
    key = attachment_key(ulid, name)
    if not store.list(key, 1) or store.list(key, 1)[0].key != key:
        raise ToolError(f"no attachment {name!r} on {ulid}")
    return dict(url=store.presign(key, name.partition("-")[2] or name))


def run_list_tags(services: Services, args: Mapping[str, object]) -> object:
    """Count messages per tag.

    Args:
        services: The services.
        args: The tool arguments (none).

    Returns:
        The counts.
    """
    return dict(tags=list_tags(services.context.store, services.cache))


SEARCH = Tool(
    name="search",
    description=(
        "Search the mailbox. All query words must match (subject, addresses, "
        "body, attachment names). Filters combine with AND. Dates are ISO "
        "8601. Use tags=['inbox'] for the inbox, ['outbox'] for queued or "
        "stuck mail, ['ops'] for operational problems. Results are ranked by "
        "relevance when there are query words, else newest first; "
        "order='date' lists newest first anyway. In date order, pass "
        "next_cursor back as cursor (with the same other arguments) for the "
        "next page; it is null on the last page."
    ),
    schema=schema(
        {
            "query": TEXT,
            "from": TEXT,
            "to": TEXT,
            "after": TEXT,
            "before": TEXT,
            "tags": TEXT_LIST,
            "has_attachments": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "order": {"type": "string", "enum": ["date", "relevance"]},
            "cursor": {
                "type": "string",
                "description": "next_cursor from a previous date-order search.",
            },
        },
        [],
    ),
    run=run_search,
)


GET_MESSAGE = Tool(
    name="get_message",
    description="Read a message: headers, body text, attachment list and tags.",
    schema=schema({"id": MESSAGE_ID}, ["id"]),
    run=run_get_message,
)


GET_ATTACHMENT = Tool(
    name="get_attachment",
    description="Get a short-lived download URL for an attachment.",
    schema=schema({"id": MESSAGE_ID, "name": TEXT}, ["id", "name"]),
    run=run_get_attachment,
)


LIST_TAGS = Tool(
    name="list_tags",
    description="List every tag with its message count.",
    schema=schema({}, []),
    run=run_list_tags,
)

READ_TOOLS = (SEARCH, GET_MESSAGE, GET_ATTACHMENT, LIST_TAGS)
