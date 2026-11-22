"""Tests for SES events, the reconciler and ops messages."""

import unittest
from collections.abc import Mapping, Sequence

from hamcrest import assert_that, contains_string, equal_to, has_item, has_length

from .. import jsonvalue
from ..ops import Problem, report_problem
from ..outbox import Status, activate, read_state
from ..ports import Context
from ..reconcile import reconcile
from ..sesevents import confirm
from .fakes import body, drop, keys, make_world, post, sample_raw


def ses_event(
    kind: str, outbox_id: str, extra: Mapping[str, object]
) -> Mapping[str, object]:
    """Build an SES event as EventBridge delivers it.

    Args:
        kind: The detail-type, such as ``Email Sent``.
        outbox_id: The outbox tag value.
        extra: Extra detail members.

    Returns:
        The event.
    """
    mail = {"messageId": "ses-1", "tags": {"outbox_id": [outbox_id]}}
    return {"source": "aws.ses", "detail-type": kind, "detail": {"mail": mail, **extra}}


def run_reconciler(context: Context) -> Sequence[str]:
    """Run the reconciler with a dispatcher that does nothing.

    Args:
        context: The flow context.

    Returns:
        The ops messages written.
    """
    return list(reconcile(context, lambda event: None))


def statuses(context: Context, *outbox_ids: str) -> Sequence[Status]:
    """Read several outbox statuses.

    Args:
        context: The flow context.
        *outbox_ids: The messages.

    Returns:
        Their statuses.
    """
    return [read_state(context, outbox_id).status for outbox_id in outbox_ids]


class TestEvents(unittest.TestCase):
    """SES events move the outbox forward."""

    def setUp(self) -> None:
        """Queue and send one message."""
        world = make_world(self)
        self.context = world.context
        self.outbox_id = post(world, "Hi")
        activate(self.context, self.outbox_id)

    def deliver_event(self, kind: str, extra: Mapping[str, object]) -> bool:
        """Deliver an SES event for the queued message.

        Args:
            kind: The detail-type.
            extra: Extra detail members.

        Returns:
            Whether the event was for a known message.
        """
        return confirm(self.context, ses_event(kind, self.outbox_id, extra))

    def test_delivery(self) -> None:
        """Delivery before Send still records the send."""
        assert_that(self.deliver_event("Email Delivered", {}), equal_to(True))
        self.deliver_event("Email Sent", {})
        assert_that(
            statuses(self.context, self.outbox_id), equal_to([Status.DELIVERED])
        )

    def test_bounce(self) -> None:
        """A bounce records who bounced."""
        bounced = [{"emailAddress": "x@example.com"}]
        detail = {
            "bounceType": "Permanent",
            "bounceSubType": "General",
            "bouncedRecipients": bounced,
        }
        self.deliver_event("Email Bounced", {"bounce": detail})
        state = read_state(self.context, self.outbox_id)
        assert_that(state.error, equal_to("Permanent/General: x@example.com"))

    def test_reject_and_foreign(self) -> None:
        """Rejects are terminal; events for other mail are ignored."""
        self.deliver_event("Email Rejected", {"eventType": "Reject"})
        assert_that(statuses(self.context, self.outbox_id), equal_to([Status.REJECTED]))
        foreign = {
            "source": "aws.ses",
            "detail-type": "Email Sent",
            "detail": {"mail": {}},
        }
        assert_that(confirm(self.context, foreign), equal_to(False))


class TestReconcile(unittest.TestCase):
    """Everything stale gets retried, and problems become mail."""

    def test_outbox_retries(self) -> None:
        """Stale queued, claimed and accepted messages all move on."""
        world = make_world(self)
        context = world.context
        queued, accepted, fresh = (post(world, subject) for subject in "123")
        activate(context, accepted)
        world.clock.advance(1800)
        post(world, "4")
        for _ in range(2):
            run_reconciler(context)
            world.clock.advance(1800)
        assert_that(
            statuses(context, queued, accepted, fresh), equal_to([Status.SENT] * 3)
        )

    def test_report_once_a_day(self) -> None:
        """The same problem is mailed at most once a day."""
        context = make_world(self).context
        problem = Problem(key="k", subject="s", details="d")
        assert_that(report_problem(context, problem), has_length(26))
        assert_that(report_problem(context, problem), equal_to(""))

    def test_stuck_message_reports(self) -> None:
        """A message that keeps failing produces one ops message per day."""
        world = make_world(self)
        world.sender.fail_with(OSError("SES down"))
        stuck = post(world, "1")
        written: list[str] = []
        for _ in range(4):
            world.clock.advance(1800)
            written.extend(run_reconciler(world.context))
        assert_that(read_state(world.context, stuck).attempts, equal_to(4))
        assert_that(written, has_length(1))
        report = body(world, f"msgs/{written[0]}/body.txt").decode()
        assert_that(report, contains_string("has been claimed 3 times"))

    def test_replays_failed_invocations(self) -> None:
        """Failed async invocations are replayed, then parked and reported."""
        world = make_world(self)
        for name, number in [("ok", 1), ("bad", 2)]:
            record = jsonvalue.dumps({"requestPayload": {"n": number}})
            world.store.put(f"aws/lambda/async/f/{name}", record)
        replayed: list[object] = []

        def dispatch(event: Mapping[str, object]) -> object:
            replayed.append(event)
            if event["n"] == 2:
                raise RuntimeError("still broken")
            return None

        written = [reconcile(world.context, dispatch) for _ in range(3)]
        assert_that(keys(world), has_item("failed-events/bad"))
        assert_that(written[-1], has_length(1))
        assert_that({"n": 1} in replayed, equal_to(True))

    def test_crashing_step_reports(self) -> None:
        """A crashing step becomes an ops message instead of an exception."""
        world = make_world(self)
        world.store.put("outbox/X/state.json", b"not json")
        drop(world, "old", sample_raw("x", "y", {}))
        written = run_reconciler(world.context)
        assert_that(written, has_length(1))
        headers = body(world, f"msgs/{written[0]}/headers.json").decode()
        assert_that(headers, contains_string("outbox"))
