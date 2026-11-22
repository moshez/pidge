"""Outgoing mail: the transactional outbox state machine."""

import dataclasses
import enum
from collections.abc import Callable, Sequence

from . import jsonvalue
from .compose import Draft, compose, recipients, replace_message_id
from .mimeparse import parse_message
from .ports import Context, Envelope, SendRejected
from .model import Direction, Meta, SystemTag, tag_name
from .records import NewMessage, message_key, restatus, update_meta, write_message
from .store import PreconditionFailed

CAS_ATTEMPTS = 20
OUTBOX = "outbox/"


class Status(enum.Enum):
    """Outbox states, in the order a message moves through them.

    Attributes:
        QUEUED: Written by ``send_email``; waiting to be claimed.
        SENDING: Claimed, and being (or having been) handed to SES.
        SENT: SES accepted the message.
        DELIVERED: The recipient's server accepted it.
        BOUNCED: Delivery failed.
        REJECTED: SES refused it; terminal.
    """

    QUEUED = enum.auto()
    SENDING = enum.auto()
    SENT = enum.auto()
    DELIVERED = enum.auto()
    BOUNCED = enum.auto()
    REJECTED = enum.auto()


TERMINAL = frozenset({Status.DELIVERED, Status.BOUNCED, Status.REJECTED})


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class OutboxState:
    """The contents of ``outbox/{ulid}/state.json``.

    Attributes:
        status: Where the message is in the state machine.
        updated: When the status last changed, in epoch seconds.
        attempts: How many times sending was claimed.
        recipients: Every envelope recipient.
        ses_message_id: The ID SES assigned, once known.
        error: Why the message was rejected or bounced.
    """

    status: Status
    updated: float
    attempts: int
    recipients: Sequence[str]
    ses_message_id: str = ""
    error: str = ""


def state_key(message_id: str) -> str:
    """Return the key of an outbox state record.

    Args:
        message_id: The ULID.

    Returns:
        The S3 key.
    """
    return f"{OUTBOX}{message_id}/state.json"


def mime_key(message_id: str) -> str:
    """Return the key of an outbox message's MIME.

    Args:
        message_id: The ULID.

    Returns:
        The S3 key.
    """
    return f"{OUTBOX}{message_id}/message.eml"


def read_mime(context: Context, message_id: str) -> bytes:
    """Read an outbox message's MIME.

    Args:
        context: The flow context.
        message_id: The ULID.

    Returns:
        The raw message.
    """
    return context.store.get(mime_key(message_id)).body


def state_json(state: OutboxState) -> bytes:
    """Serialize a state record.

    Args:
        state: The record.

    Returns:
        The JSON bytes.
    """
    return jsonvalue.dumps(
        dict(
            status=state.status.name.lower(),
            updated=state.updated,
            attempts=state.attempts,
            recipients=list(state.recipients),
            ses_message_id=state.ses_message_id,
            error=state.error,
        )
    )


def parse_state(data: bytes) -> OutboxState:
    """Parse a state record.

    Args:
        data: The JSON bytes.

    Returns:
        The record.
    """
    parsed = jsonvalue.loads_dict(data)
    return OutboxState(
        status=Status[jsonvalue.get_str(parsed, "status").upper()],
        updated=jsonvalue.get_number(parsed, "updated"),
        attempts=int(jsonvalue.get_number(parsed, "attempts")),
        recipients=tuple(jsonvalue.string_list(parsed, "recipients")),
        ses_message_id=jsonvalue.get_str(parsed, "ses_message_id"),
        error=jsonvalue.get_str(parsed, "error"),
    )


def read_state(context: Context, message_id: str) -> OutboxState:
    """Read an outbox state record.

    Args:
        context: The flow context.
        message_id: The ULID.

    Returns:
        The record.
    """
    return parse_state(context.store.get(state_key(message_id)).body)


Change = Callable[[OutboxState], OutboxState | None]


def transition(context: Context, message_id: str, change: Change) -> OutboxState | None:
    """Apply a change to an outbox state record with compare-and-swap.

    Args:
        context: The flow context.
        message_id: The ULID.
        change: Returns the new state, or None to leave the record alone.

    Returns:
        The new state, or None if the change declined.

    Raises:
        PreconditionFailed: If every attempt lost a race.
    """
    key = state_key(message_id)
    for _ in range(CAS_ATTEMPTS):
        current = context.store.get(key)
        updated = change(parse_state(current.body))
        if updated is None:
            return None
        try:
            context.store.replace(key, state_json(updated), current.etag)
        except PreconditionFailed:
            continue
        return updated
    raise PreconditionFailed(key)


def enqueue(context: Context, draft: Draft) -> str:
    """Queue a message for sending.

    Args:
        context: The flow context.
        draft: The message.

    Returns:
        The outbox (and message) ULID.
    """
    message_id = context.new_id()
    raw = compose(context, draft)
    state = OutboxState(
        status=Status.QUEUED,
        updated=context.clock(),
        attempts=0,
        recipients=tuple(recipients(draft)),
    )
    context.store.put_new(state_key(message_id), state_json(state))
    meta = Meta(
        tags=(tag_name(SystemTag.OUTBOX),), direction=Direction.OUT, status="queued"
    )
    record = NewMessage(raw=raw, parsed=parse_message(raw), meta=meta, bcc=draft.blind)
    write_message(context.store, message_id, record)
    context.store.put(mime_key(message_id), raw)
    return message_id


def claim(now: float) -> Change:
    """Build the change that claims a pending message for sending.

    Args:
        now: The current time.

    Returns:
        The change.
    """

    def change(state: OutboxState) -> OutboxState | None:
        if state.status is not Status.QUEUED:
            return None
        return dataclasses.replace(
            state, status=Status.SENDING, updated=now, attempts=state.attempts + 1
        )

    return change


def advance(status: Status, now: float, detail: str) -> Change:
    """Build a change that moves the state forward, never backward.

    Args:
        status: The target status.
        now: The current time.
        detail: The SES message ID for SENT, or an error for other states.

    Returns:
        The change.
    """

    def change(state: OutboxState) -> OutboxState | None:
        if state.status.value >= status.value or state.status in TERMINAL:
            return None
        moved = dataclasses.replace(state, status=status, updated=now)
        if status is Status.SENT:
            return dataclasses.replace(moved, ses_message_id=detail)
        return dataclasses.replace(moved, error=detail)

    return change


def record_ses_id(ses_message_id: str) -> Change:
    """Build the change that remembers the ID SES returned from SendEmail.

    Args:
        ses_message_id: The ID.

    Returns:
        The change.
    """

    def change(state: OutboxState) -> OutboxState | None:
        if state.status is not Status.SENDING or state.ses_message_id:
            return None
        return dataclasses.replace(state, ses_message_id=ses_message_id)

    return change


def reject(context: Context, message_id: str, reason: str) -> None:
    """Move a message to the terminal ``rejected`` state.

    Args:
        context: The flow context.
        message_id: The ULID.
        reason: Why SES refused it.
    """
    transition(context, message_id, advance(Status.REJECTED, context.clock(), reason))
    update_meta(
        context.store,
        message_id,
        restatus(
            [tag_name(SystemTag.REJECTED)], [tag_name(SystemTag.OUTBOX)], "rejected"
        ),
    )


def activate(context: Context, message_id: str) -> None:
    """Claim a pending message and hand it to SES.

    Args:
        context: The flow context.
        message_id: The ULID.

    Raises:
        Exception: A failed send, after releasing the claim for Lambda's retry.
    """
    claimed = transition(context, message_id, claim(context.clock()))
    if claimed is None:
        return
    raw = read_mime(context, message_id)
    envelope = Envelope(recipients=claimed.recipients, tags=dict(outbox_id=message_id))
    try:
        ses_message_id = context.sender.send(raw, envelope)
    except SendRejected as error:
        reject(context, message_id, str(error))
        return
    except Exception:
        release(context, message_id, claimed)
        raise
    transition(context, message_id, record_ses_id(ses_message_id))


def mark_sent(context: Context, message_id: str, ses_message_id: str) -> None:
    """Record that SES accepted a message, and store the final sent copy.

    The stored copy gets the Message-ID that SES assigned, so replies thread.
    Every step is idempotent.

    Args:
        context: The flow context.
        message_id: The ULID.
        ses_message_id: The ID SES assigned.
    """
    transition(
        context, message_id, advance(Status.SENT, context.clock(), ses_message_id)
    )
    assigned = f"<{ses_message_id}@{context.settings.message_id_domain}>"
    raw = replace_message_id(read_mime(context, message_id), assigned)
    context.store.put(message_key(message_id, "raw.eml"), raw)
    record_key = message_key(message_id, "headers.json")
    record = dict(jsonvalue.loads_dict(context.store.get(record_key).body))
    record["message_id"] = assigned
    context.store.put(record_key, jsonvalue.dumps(record))
    update_meta(
        context.store,
        message_id,
        restatus([tag_name(SystemTag.SENT)], [tag_name(SystemTag.OUTBOX)], "sent"),
    )


def finish(context: Context, message_id: str, status: Status, detail: str) -> None:
    """Record a delivery or bounce.

    Args:
        context: The flow context.
        message_id: The ULID.
        status: DELIVERED or BOUNCED.
        detail: The bounce description, or empty.
    """
    transition(context, message_id, advance(status, context.clock(), detail))
    extra = [tag_name(SystemTag.BOUNCED)] if status is Status.BOUNCED else []
    update_meta(context.store, message_id, restatus(extra, [], status.name.lower()))


def unclaim(updated: float, now: float) -> Change:
    """Build the change that returns a stale ``sending`` message to the queue.

    Args:
        updated: The ``updated`` time the reconciler saw.
        now: The current time.

    Returns:
        The change; it declines if the record moved on meanwhile.
    """

    def change(state: OutboxState) -> OutboxState | None:
        if state.status is not Status.SENDING or state.updated != updated:
            return None
        return dataclasses.replace(state, status=Status.QUEUED, updated=now)

    return change


def release(context: Context, message_id: str, claimed: OutboxState) -> None:
    """Return a claimed message to the queue, unless it moved on meanwhile.

    Args:
        context: The flow context.
        message_id: The ULID.
        claimed: The claimed state.
    """
    transition(context, message_id, unclaim(claimed.updated, context.clock()))


def requeue(context: Context, message_id: str, state: OutboxState) -> None:
    """Retry a stale message: queue it again if it was claimed, then send.

    Args:
        context: The flow context.
        message_id: The ULID.
        state: The stale state the reconciler saw.
    """
    if state.status is Status.SENDING:
        release(context, message_id, state)
    activate(context, message_id)
