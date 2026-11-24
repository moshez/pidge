"""The MCP tools that change the mailbox."""

from collections.abc import Mapping, Sequence
from email.utils import formataddr, getaddresses, parseaddr

from . import arguments, jsonvalue
from .compose import AttachmentRef, Draft
from .outbox import enqueue
from .ports import Settings
from .store import NotFound
from .tagging import TagError, tag_email
from .toolkit import MESSAGE_ID, TEXT, TEXT_LIST, Services, Tool, ToolError, schema


def attachment_refs(args: Mapping[str, object]) -> Sequence[AttachmentRef]:
    """Read the ``attach_from`` argument.

    Args:
        args: The tool arguments.

    Returns:
        The attachment references.

    Raises:
        ToolError: If the argument is malformed.
    """
    try:
        items = [
            jsonvalue.as_dict(item) for item in jsonvalue.get_list(args, "attach_from")
        ]
    except jsonvalue.JSONShapeError:
        raise ToolError("attach_from must be a list of {id, name} objects") from None
    return [
        AttachmentRef(
            message=arguments.message_id(item, "id"),
            name=arguments.required_text(item, "name"),
        )
        for item in items
    ]


def sender_header(settings: Settings, requested: str) -> str:
    """Validate the ``from`` argument and build the From header.

    Args:
        settings: The deployment settings.
        requested: The requested sender, or empty for the default.

    Returns:
        The From header, or empty to use the configured address.

    Raises:
        ToolError: If the sender is malformed or outside the mail domain.
    """
    if not requested:
        return ""
    if "\r" in requested or "\n" in requested:
        raise ToolError("from must not contain line breaks")
    pairs = getaddresses([requested])
    name, address = pairs[0]
    local, _, domain = address.rpartition("@")
    if len(pairs) != 1 or not local or domain.lower() != settings.mail_domain.lower():
        raise ToolError(f"from must be one address under {settings.mail_domain}")
    default_name, _ = parseaddr(settings.from_address)
    return formataddr((name or default_name, address))


def run_send_email(services: Services, args: Mapping[str, object]) -> object:
    """Queue a message for sending.

    Args:
        services: The services.
        args: The tool arguments.

    Returns:
        The outbox ID.

    Raises:
        ToolError: If there are no recipients or the sender is invalid.
    """
    reply_to = arguments.text(args, "in_reply_to_id")
    draft = Draft(
        to=arguments.strings(args, "to"),
        cc=arguments.strings(args, "cc"),
        blind=arguments.strings(args, "bcc"),
        subject=arguments.required_text(args, "subject"),
        body=arguments.text(args, "body"),
        in_reply_to=arguments.message_id(args, "in_reply_to_id") if reply_to else "",
        attachments=attachment_refs(args),
        sender=sender_header(services.context.settings, arguments.text(args, "from")),
    )
    if not draft.to:
        raise ToolError("to must list at least one address")
    outbox_id = enqueue(services.context, draft)
    return dict(outbox_id=outbox_id, status="pending")


def run_tag_email(services: Services, args: Mapping[str, object]) -> object:
    """Change a message's tags.

    Args:
        services: The services.
        args: The tool arguments.

    Returns:
        The new tag set.

    Raises:
        ToolError: If the message does not exist or a tag is invalid.
    """
    ulid = arguments.message_id(args, "id")
    try:
        tags = tag_email(
            services.context.store,
            ulid,
            arguments.strings(args, "add"),
            arguments.strings(args, "remove"),
        )
    except NotFound:
        raise ToolError(f"no message {ulid}") from None
    except TagError as error:
        raise ToolError(str(error)) from None
    return dict(id=ulid, tags=list(tags))


SEND_EMAIL = Tool(
    name="send_email",
    description=(
        "Queue an email for sending; returns its outbox id at once. For a "
        "reply, pass in_reply_to_id so threading headers are set. attach_from "
        "copies attachments of stored messages. from sets the sender, which must "
        "be under the mail domain (a bare address or 'Name <address>'); it "
        "defaults to the mailbox address."
    ),
    schema=schema(
        {
            "to": TEXT_LIST,
            "cc": TEXT_LIST,
            "bcc": TEXT_LIST,
            "subject": TEXT,
            "body": TEXT,
            "in_reply_to_id": MESSAGE_ID,
            "from": TEXT,
            "attach_from": {
                "type": "array",
                "items": schema({"id": MESSAGE_ID, "name": TEXT}, ["id", "name"]),
            },
        },
        ["to", "subject", "body"],
    ),
    run=run_send_email,
)


TAG_EMAIL = Tool(
    name="tag_email",
    description=("Add or remove tags. Archive by removing 'inbox'; a folder is a tag."),
    schema=schema({"id": MESSAGE_ID, "add": TEXT_LIST, "remove": TEXT_LIST}, ["id"]),
    run=run_tag_email,
)

WRITE_TOOLS = (SEND_EMAIL, TAG_EMAIL)
