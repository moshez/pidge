"""The MCP tools that change the mailbox."""

import re
from collections.abc import Mapping, Sequence
from email.utils import formataddr, getaddresses, parseaddr

from . import arguments, jsonvalue, ulid
from .compose import AttachmentRef, Draft, MissingAttachment, UploadRef
from .outbox import enqueue
from .ports import Settings
from .store import NotFound
from .tagging import TagError, tag_email
from .toolkit import MESSAGE_ID, TEXT, TEXT_LIST, Services, Tool, ToolError, schema
from .uploads import Upload, reserve, upload_key

# SES takes messages up to 40 MB after encoding; base64 adds a third.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
UPLOAD_SECONDS = 15 * 60
CONTENT_TYPE = re.compile(r"\A[\w.+-]+/[\w.+-]+\Z")
DEFAULT_CONTENT_TYPE = "application/octet-stream"
ATTACH_SHAPE = "attach_from must be a list of {id, name} or {upload} objects"


def parse_attach_item(item: Mapping[str, object]) -> AttachmentRef | UploadRef:
    """Read one ``attach_from`` entry.

    Args:
        item: ``{id, name}`` for a stored attachment, or ``{upload}``.

    Returns:
        The reference.

    Raises:
        ToolError: If the entry is malformed.
    """
    if "upload" not in item:
        return AttachmentRef(
            message=arguments.message_id(item, "id"),
            name=arguments.required_text(item, "name"),
        )
    upload = arguments.text(item, "upload")
    if "id" in item or "name" in item:
        raise ToolError(ATTACH_SHAPE)
    if not ulid.is_ulid(upload):
        raise ToolError("upload must be an upload_id from put_attachment")
    return UploadRef(upload=upload)


def attachment_refs(args: Mapping[str, object]) -> Sequence[AttachmentRef | UploadRef]:
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
        raise ToolError(ATTACH_SHAPE) from None
    return [parse_attach_item(item) for item in items]


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


def queue_draft(services: Services, draft: Draft) -> str:
    """Queue a draft, reporting a missing attachment as a tool error.

    Args:
        services: The services.
        draft: The draft.

    Returns:
        The outbox ID.

    Raises:
        ToolError: If an attachment to copy does not exist.
    """
    try:
        return enqueue(services.context, draft)
    except MissingAttachment as error:
        raise ToolError(str(error)) from None


def run_send_email(services: Services, args: Mapping[str, object]) -> object:
    """Queue a message for sending.

    Args:
        services: The services.
        args: The tool arguments.

    Returns:
        The outbox ID.

    Raises:
        ToolError: If there are no recipients, the sender is invalid or an
            attachment to copy does not exist.
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
    outbox_id = queue_draft(services, draft)
    return dict(outbox_id=outbox_id, status="pending")


def upload_request(args: Mapping[str, object]) -> Upload:
    """Read the ``put_attachment`` arguments.

    Args:
        args: The tool arguments.

    Returns:
        The upload's metadata.

    Raises:
        ToolError: If an argument is missing or malformed.
    """
    name = arguments.required_text(args, "name")
    if not name.isprintable() or "/" in name or "\\" in name:
        raise ToolError("name must be a file name, without slashes")
    content_type = arguments.text(args, "content_type") or DEFAULT_CONTENT_TYPE
    if not CONTENT_TYPE.match(content_type):
        raise ToolError("content_type must be a MIME type, such as application/pdf")
    if "size" not in args:
        raise ToolError("size is required")
    size = arguments.bounded(args, "size", MAX_UPLOAD_BYTES)
    return Upload(name=name, content_type=content_type, size=size)


def run_put_attachment(services: Services, args: Mapping[str, object]) -> object:
    """Reserve an upload and presign its PUT URL.

    Args:
        services: The services.
        args: The tool arguments.

    Returns:
        The upload ID, the URL, and how long it works.
    """
    upload = upload_request(args)
    context = services.context
    upload_id = context.new_id()
    reserve(context.store, upload_id, upload)
    key = upload_key(upload_id, "data")
    return dict(
        upload_id=upload_id,
        url=context.store.presign_upload(key, upload.size, UPLOAD_SECONDS),
        method="PUT",
        size=upload.size,
        expires_in=UPLOAD_SECONDS,
    )


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
        "lists files to attach: {id, name} copies an attachment of a stored "
        "message; {upload} attaches a file uploaded through put_attachment "
        "(PUT it first). from sets the sender, which must "
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
                "items": schema(
                    {
                        "id": MESSAGE_ID,
                        "name": TEXT,
                        "upload": {
                            "type": "string",
                            "description": "upload_id from put_attachment.",
                        },
                    },
                    [],
                ),
            },
        },
        ["to", "subject", "body"],
    ),
    run=run_send_email,
)


PUT_ATTACHMENT = Tool(
    name="put_attachment",
    description=(
        "Upload a new file to attach to an email. Give its name, exact size "
        "in bytes (at most 25 MiB) and content_type; get back upload_id and "
        "a URL that works for 15 minutes. PUT exactly size bytes to the URL "
        "with no Authorization header (for example curl -T file URL); S3 "
        "refuses any other size. Then pass {upload: upload_id} in "
        "send_email's attach_from. Uploads are deleted after 7 days."
    ),
    schema=schema(
        {
            "name": {"type": "string", "description": "The attachment's file name."},
            "size": {"type": "integer", "minimum": 1, "maximum": MAX_UPLOAD_BYTES},
            "content_type": {
                "type": "string",
                "description": "MIME type (default application/octet-stream).",
            },
        },
        ["name", "size"],
    ),
    run=run_put_attachment,
)


TAG_EMAIL = Tool(
    name="tag_email",
    description=("Add or remove tags. Archive by removing 'inbox'; a folder is a tag."),
    schema=schema({"id": MESSAGE_ID, "add": TEXT_LIST, "remove": TEXT_LIST}, ["id"]),
    run=run_tag_email,
)

WRITE_TOOLS = (SEND_EMAIL, PUT_ATTACHMENT, TAG_EMAIL)
