"""The 15-minute reconciler: everything that fixes itself."""

import contextlib
import traceback
from collections.abc import Callable, Mapping, Sequence

from . import jsonvalue
from .ingest import sweep_inbound
from .ops import Problem, report_problem
from .outbox import OUTBOX, OutboxState, Status, mark_sent, read_state, requeue
from .ports import Context
from .store import NotFound

STALE_SECONDS = 1800
STUCK_ATTEMPTS = 3
MAX_REPLAYS = 3
FAILED_EVENTS = "aws/lambda/async/"
PARKED_EVENTS = "failed-events/"
LIST_LIMIT = 100_000

Dispatch = Callable[[Mapping[str, object]], object]


def stuck_problem(message_id: str, state: OutboxState) -> Problem:
    """Describe an outbox message that keeps failing to send.

    Args:
        message_id: The ULID.
        state: Its state.

    Returns:
        The problem report.
    """
    return Problem(
        key=f"outbox-stuck-{message_id}",
        subject=f"Outgoing message {message_id} is stuck",
        details=(
            f"Message {message_id} has been claimed {state.attempts} times "
            f"without SES accepting it. It is still {state.status.name.lower()}; "
            "the reconciler keeps retrying."
        ),
    )


def check_outbox(context: Context, message_id: str) -> Problem | None:
    """Retry one outbox message if it is stale.

    Args:
        context: The flow context.
        message_id: The ULID.

    Returns:
        A problem report if the message looks stuck.
    """
    state = read_state(context, message_id)
    now = context.clock()
    if state.status not in (Status.QUEUED, Status.SENDING):
        return None
    if now - state.updated < STALE_SECONDS:
        return None
    if state.status is Status.SENDING and state.ses_message_id:
        mark_sent(context, message_id, state.ses_message_id)
        return None
    requeue(context, message_id, state)
    return (
        stuck_problem(message_id, state) if state.attempts >= STUCK_ATTEMPTS else None
    )


def reconcile_outbox(context: Context) -> Sequence[Problem]:
    """Retry stale outbox messages.

    Args:
        context: The flow context.

    Returns:
        Problems found.
    """
    reports = [
        check_outbox(context, listed.key.split("/")[1])
        for listed in context.store.list(OUTBOX, LIST_LIMIT)
        if listed.key.endswith("/state.json")
    ]
    return [report for report in reports if report is not None]


def park(context: Context, key: str, error: Exception) -> Problem:
    """Give up on a failed invocation: keep its record and report it.

    Args:
        context: The flow context.
        key: The failure record's key.
        error: The last replay error.

    Returns:
        The problem report.
    """
    name = key.rsplit("/", 1)[-1]
    body = context.store.get(key).body
    context.store.put(PARKED_EVENTS + name, body)
    context.store.delete(key)
    return Problem(
        key=f"invocation-failed-{name}",
        subject="An event failed processing repeatedly",
        details=(
            f"Lambda gave up on an asynchronous event, and replaying it failed "
            f"{MAX_REPLAYS} times. The record is kept at {PARKED_EVENTS}{name}."
            f"\n\nLast error: {error!r}\n\nRecord:\n{body.decode('utf-8', 'replace')}"
        ),
    )


def replay_one(context: Context, key: str, dispatch: Dispatch) -> Problem | None:
    """Replay one failed asynchronous invocation.

    Args:
        context: The flow context.
        key: The failure record's key.
        dispatch: Runs an event through the handler.

    Returns:
        A problem report if replays are exhausted.
    """
    found = context.store.get(key)
    record = jsonvalue.loads_dict(found.body)
    replays = jsonvalue.next_count(record, "pidgeReplays")
    try:
        dispatch(jsonvalue.get_dict(record, "requestPayload"))
    except Exception as error:
        if replays >= MAX_REPLAYS:
            return park(context, key, error)
        updated = dict(record, pidgeReplays=replays, pidgeError=repr(error))
        context.store.replace(key, jsonvalue.dumps(updated), found.etag)
        return None
    context.store.delete(key)
    return None


def replay_failures(context: Context, dispatch: Dispatch) -> Sequence[Problem]:
    """Replay every invocation that Lambda gave up on.

    Args:
        context: The flow context.
        dispatch: Runs an event through the handler.

    Returns:
        Problems found.
    """
    reports = []
    for listed in context.store.list(FAILED_EVENTS, LIST_LIMIT):
        with contextlib.suppress(NotFound):
            reports.append(replay_one(context, listed.key, dispatch))
    return [report for report in reports if report is not None]


def sweep_step(context: Context) -> Sequence[Problem]:
    """Retry unfinished inbound messages.

    Args:
        context: The flow context.

    Returns:
        No problems; poison messages report themselves when quarantined.
    """
    sweep_inbound(context)
    return []


def guarded(
    context: Context, name: str, step: Callable[[], Sequence[Problem]]
) -> Sequence[Problem]:
    """Run one reconciler step, turning a crash into a problem report.

    Args:
        context: The flow context.
        name: The step's name.
        step: The step.

    Returns:
        The step's problems, or one describing its crash.
    """
    try:
        return step()
    except Exception:
        return [
            Problem(
                key=f"reconcile-{name}-failed",
                subject=f"The reconciler step {name!r} crashed",
                details=traceback.format_exc(),
            )
        ]


def reconcile(context: Context, dispatch: Dispatch) -> Sequence[str]:
    """Run every reconciler step and report problems into the mailbox.

    Args:
        context: The flow context.
        dispatch: Runs an event through the handler (for replays).

    Returns:
        The IDs of ops messages written.
    """
    problems = [
        *guarded(context, "inbound", lambda: sweep_step(context)),
        *guarded(context, "outbox", lambda: reconcile_outbox(context)),
        *guarded(context, "replay", lambda: replay_failures(context, dispatch)),
    ]
    written = [report_problem(context, problem) for problem in problems]
    return [message_id for message_id in written if message_id]
