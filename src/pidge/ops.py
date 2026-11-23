"""Operational problems become messages in the mailbox, tagged ``ops``."""

import dataclasses
import time

from .mimeparse import new_message, parse_message
from .ports import Context
from .model import Direction, Meta, SystemTag, tag_name
from .records import NewMessage, write_message
from .store import PreconditionFailed

OPS_SENT = "ops-sent/"


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Problem:
    """Something that needs a human (or an agent) to look at it.

    Attributes:
        key: A stable identifier; one message per key per day is written.
        subject: A one-line summary.
        details: The full explanation.
    """

    key: str
    subject: str
    details: str


def build_report(context: Context, problem: Problem) -> bytes:
    """Build the synthetic message for a problem.

    Args:
        context: The flow context.
        problem: The problem.

    Returns:
        The raw message.
    """
    settings = context.settings
    sender = f"Pidge ops <pidge-ops@{settings.mail_domain}>"
    message = new_message(
        context.clock(), settings.mail_domain, sender, settings.owner_address
    )
    message["Subject"] = f"[pidge ops] {problem.subject}"
    message.set_content(f"{problem.details}\n\nProblem key: {problem.key}\n")
    return message.as_bytes()


def report_problem(context: Context, problem: Problem) -> str:
    """Write a problem into the mailbox, at most once per key per day.

    Args:
        context: The flow context.
        problem: The problem.

    Returns:
        The new message's ID, or an empty string if already reported today.
    """
    day = time.strftime("%Y-%m-%d", time.gmtime(context.clock()))
    try:
        context.store.put_new(f"{OPS_SENT}{problem.key}/{day}", b"")
    except PreconditionFailed:
        return ""
    raw = build_report(context, problem)
    tags = (tag_name(SystemTag.INBOX), tag_name(SystemTag.OPS))
    meta = Meta(tags=tags, direction=Direction.IN, status="")
    ulid = context.new_id()
    write_message(
        context.store, ulid, NewMessage(raw=raw, parsed=parse_message(raw), meta=meta)
    )
    return ulid
