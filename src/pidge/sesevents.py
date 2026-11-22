"""SES sending events (from EventBridge) drive the outbox forward."""

from collections.abc import Callable, Mapping

from . import jsonvalue
from .outbox import Status, finish, mark_sent, reject
from .ports import Context

Handler = Callable[[Context, str, str, Mapping[str, object]], None]

DETAIL_TYPES: Mapping[str, str] = {
    "Email Sent": "Send",
    "Email Delivered": "Delivery",
    "Email Bounced": "Bounce",
    "Email Rejected": "Reject",
}


def on_send(
    context: Context, message_id: str, ses_id: str, detail: Mapping[str, object]
) -> None:
    """Handle a Send event.

    Args:
        context: The flow context.
        message_id: The outbox ULID.
        ses_id: The SES message ID.
        detail: The event detail.
    """
    mark_sent(context, message_id, ses_id)


def on_delivery(
    context: Context, message_id: str, ses_id: str, detail: Mapping[str, object]
) -> None:
    """Handle a Delivery event.

    Args:
        context: The flow context.
        message_id: The outbox ULID.
        ses_id: The SES message ID.
        detail: The event detail.
    """
    mark_sent(context, message_id, ses_id)
    finish(context, message_id, Status.DELIVERED, "")


def on_bounce(
    context: Context, message_id: str, ses_id: str, detail: Mapping[str, object]
) -> None:
    """Handle a Bounce event.

    Args:
        context: The flow context.
        message_id: The outbox ULID.
        ses_id: The SES message ID.
        detail: The event detail.
    """
    bounce = jsonvalue.get_dict(detail, "bounce")
    kind = jsonvalue.get_str(bounce, "bounceType", "Undetermined")
    sub = jsonvalue.get_str(bounce, "bounceSubType")
    who = jsonvalue.pluck(bounce, "bouncedRecipients", "emailAddress")
    mark_sent(context, message_id, ses_id)
    finish(context, message_id, Status.BOUNCED, f"{kind}/{sub}: {', '.join(who)}")


def on_reject(
    context: Context, message_id: str, ses_id: str, detail: Mapping[str, object]
) -> None:
    """Handle a Reject event.

    Args:
        context: The flow context.
        message_id: The outbox ULID.
        ses_id: The SES message ID.
        detail: The event detail.
    """
    reason = jsonvalue.get_str(jsonvalue.get_dict(detail, "reject"), "reason")
    reject(context, message_id, reason or "rejected by SES")


HANDLERS: Mapping[str, Handler] = {
    "Send": on_send,
    "Delivery": on_delivery,
    "Bounce": on_bounce,
    "Reject": on_reject,
}


def confirm(context: Context, event: Mapping[str, object]) -> bool:
    """Apply one SES event from EventBridge.

    Args:
        context: The flow context.
        event: The EventBridge event.

    Returns:
        True if the event was for an outbox message and was applied.
    """
    detail = jsonvalue.get_dict(event, "detail")
    fallback = DETAIL_TYPES.get(jsonvalue.get_str(event, "detail-type"), "")
    handler = HANDLERS.get(jsonvalue.get_str(detail, "eventType", fallback))
    mail = jsonvalue.get_dict(detail, "mail")
    tags = jsonvalue.get_dict(mail, "tags")
    outbox_ids = jsonvalue.string_list(tags, "outbox_id")
    if handler is None or not outbox_ids:
        return False
    handler(context, outbox_ids[0], jsonvalue.get_str(mail, "messageId"), detail)
    return True
