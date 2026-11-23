"""Inbound mail: parse what SES dropped under ``inbound/`` into ``msgs/``."""

import contextlib
from collections.abc import Sequence

from . import jsonvalue
from .docstore import maybe_trigger_index
from .mimeparse import Headers, ParsedMessage, parse_message
from .ops import Problem, report_problem
from .ports import Context
from .model import Direction, Meta, SystemTag, tag_name
from .records import NewMessage, read_json, write_message
from .store import PreconditionFailed

INBOUND = "inbound/"
SEEN = "inbound-seen/"
DONE = "inbound-done/"
FAILURES = "inbound-failures/"
SETUP_NOTIFICATION = "AMAZON_SES_SETUP_NOTIFICATION"
MAX_ATTEMPTS = 5
SWEEP_AGE = 900
LIST_LIMIT = 100_000


def ses_id_of(key: str) -> str:
    """Return the SES message ID from an ``inbound/`` key.

    Args:
        key: The S3 key.

    Returns:
        The SES message ID.
    """
    return key.removeprefix(INBOUND)


def claim_id(context: Context, ses_id: str) -> str:
    """Allocate the message's ULID, or resume with the one already allocated.

    Args:
        context: The flow context.
        ses_id: The SES message ID.

    Returns:
        The ULID for this message.
    """
    ulid = context.new_id()
    try:
        context.store.put_new(SEEN + ses_id, ulid.encode("ascii"))
    except PreconditionFailed:
        return context.store.get(SEEN + ses_id).body.decode("ascii")
    return ulid


def store_message(context: Context, key: str, message: NewMessage) -> str:
    """Write a received message's records and mark it done.

    Args:
        context: The flow context.
        key: The ``inbound/`` key.
        message: The message.

    Returns:
        The message's ULID.
    """
    ses_id = ses_id_of(key)
    ulid = claim_id(context, ses_id)
    write_message(context.store, ulid, message)
    context.store.put(DONE + ses_id, ulid.encode("ascii"))
    maybe_trigger_index(context)
    return ulid


def ingest(context: Context, key: str) -> str:
    """Ingest one message that SES wrote to the bucket.

    Args:
        context: The flow context.
        key: The ``inbound/`` key.

    Returns:
        The message's ULID, or an empty string for SES's setup notification.
    """
    if key.endswith(SETUP_NOTIFICATION):
        context.store.put(DONE + ses_id_of(key), b"")
        return ""
    raw = context.store.get(key).body
    parsed = parse_message(raw)
    meta = Meta(tags=(tag_name(SystemTag.INBOX),), direction=Direction.IN, status="")
    return store_message(context, key, NewMessage(raw=raw, parsed=parsed, meta=meta))


def record_failure(context: Context, key: str, error: Exception) -> int:
    """Count a failed ingest attempt.

    Args:
        context: The flow context.
        key: The ``inbound/`` key.
        error: What went wrong.

    Returns:
        The number of failed attempts so far, including this one.
    """
    failure_key = FAILURES + ses_id_of(key)
    attempts = jsonvalue.next_count(read_json(context.store, failure_key), "attempts")
    record = dict(attempts=attempts, error=repr(error))
    context.store.put(failure_key, jsonvalue.dumps(record))
    return attempts


def stub_headers(date: int) -> Headers:
    """Build placeholder headers for a message that cannot be parsed.

    Args:
        date: The time to record as the message date.

    Returns:
        The headers.
    """
    return Headers(
        sender="",
        to=(),
        cc=(),
        subject="(message could not be parsed)",
        date=date,
        message_id="",
        in_reply_to="",
        references="",
    )


def stub_message(context: Context, key: str, error: Exception) -> NewMessage:
    """Build the record for a message that cannot be parsed.

    Args:
        context: The flow context.
        key: The ``inbound/`` key.
        error: The last error.

    Returns:
        The raw bytes with stub headers, tagged ``ingest-failed``.
    """
    details = (
        "Pidge could not parse this message. The raw message is stored with "
        f"this record as raw.eml.\n\nSource: {key}\nError: {error!r}\n"
    )
    headers = stub_headers(int(context.clock()))
    tags = (tag_name(SystemTag.INBOX), tag_name(SystemTag.INGEST_FAILED))
    return NewMessage(
        raw=context.store.get(key).body,
        parsed=ParsedMessage(headers=headers, body=details, attachments=()),
        meta=Meta(tags=tags, direction=Direction.IN, status=""),
    )


def quarantine(context: Context, key: str, error: Exception) -> str:
    """Store a message that cannot be parsed, so it is visible in the mailbox.

    Args:
        context: The flow context.
        key: The ``inbound/`` key.
        error: The last error.

    Returns:
        The message's ULID.
    """
    message = stub_message(context, key, error)
    ulid = store_message(context, key, message)
    problem = Problem(
        key=f"ingest-failed-{ses_id_of(key)}",
        subject="An inbound message could not be parsed",
        details=f"Message {ulid} is tagged ingest-failed.\n\n{message.parsed.body}",
    )
    report_problem(context, problem)
    return ulid


def ingest_or_record(context: Context, key: str) -> str:
    """Ingest a message, counting failures and quarantining poison messages.

    Args:
        context: The flow context.
        key: The ``inbound/`` key.

    Returns:
        The message's ULID.

    Raises:
        Exception: The ingest error, while attempts remain.
    """
    try:
        return ingest(context, key)
    except Exception as error:
        if record_failure(context, key, error) < MAX_ATTEMPTS:
            raise
        return quarantine(context, key, error)


def unfinished(context: Context) -> Sequence[str]:
    """Find ``inbound/`` objects that were never fully ingested.

    Args:
        context: The flow context.

    Returns:
        Their keys, skipping objects younger than the sweep age.
    """
    store = context.store
    done = {
        ses_id_of(listed.key.replace(DONE, INBOUND, 1))
        for listed in store.list(DONE, LIST_LIMIT)
    }
    cutoff = context.clock() - SWEEP_AGE
    return [
        listed.key
        for listed in store.list(INBOUND, LIST_LIMIT)
        if listed.modified < cutoff and ses_id_of(listed.key) not in done
    ]


def sweep_inbound(context: Context) -> int:
    """Retry every unfinished inbound message.

    Args:
        context: The flow context.

    Returns:
        How many messages were retried.
    """
    waiting = unfinished(context)
    for key in waiting:
        with contextlib.suppress(Exception):
            ingest_or_record(context, key)
    return len(waiting)
