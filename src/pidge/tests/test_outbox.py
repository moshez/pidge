"""Tests for the outbound state machine and message composition."""

import unittest
from collections.abc import Callable
from email.message import EmailMessage

from hamcrest import (
    assert_that,
    contains_exactly,
    contains_string,
    equal_to,
    instance_of,
)

from ..compose import AttachmentRef, Draft, attach, replace_message_id
from ..outbox import (
    OutboxState,
    Status,
    activate,
    claim,
    enqueue,
    finish,
    mark_sent,
    read_state,
    record_ses_id,
    requeue,
    transition,
    unclaim,
)
from ..records import read_json
from ..store import PreconditionFailed
from .checks import failure, includes
from .fakes import (
    World,
    assert_tags,
    body,
    deliver,
    make_world,
    post,
    rejecting,
    sample_raw,
)
from .flaky import racing

SES_MESSAGE_ID = "<ses-1@us-east-2.amazonses.com>"


def queued_mime(world: World, outbox_id: str) -> str:
    """Read the MIME of a queued message.

    Args:
        world: The environment.
        outbox_id: The outbox ID.

    Returns:
        The message text.
    """
    return body(world, f"outbox/{outbox_id}/message.eml").decode()


class TestOutbox(unittest.TestCase):
    """Queue, claim, send, confirm."""

    def setUp(self) -> None:
        """Create a world with one received message to reply to."""
        self.world = make_world(self)
        self.context = self.world.context
        raw = sample_raw("Lunch", "Hungry?", {"m.pdf": b"%"})
        self.original = deliver(self.world, "ses1", raw)

    def state(self, outbox_id: str) -> OutboxState:
        """Read an outbox state.

        Args:
            outbox_id: The outbox ID.

        Returns:
            The state.
        """
        return read_state(self.context, outbox_id)

    def reply(self) -> str:
        """Queue a reply with an attachment and a blind copy.

        Returns:
            The outbox ID.
        """
        draft = Draft(
            to=["Alice <alice@example.com>"],
            cc=["carol@example.com"],
            blind=["secret@example.com"],
            subject="Lunch",
            body="Yes!",
            in_reply_to=self.original,
            attachments=[AttachmentRef(message=self.original, name="1-m.pdf")],
        )
        return enqueue(self.context, draft)

    def test_enqueue_threads_and_queues(self) -> None:
        """A queued reply carries threading headers and no Bcc header."""
        outbox_id = self.reply()
        raw = queued_mime(self.world, outbox_id)
        expected = [
            "In-Reply-To: <original@example.com>",
            "References: <root@example.com> <original@example.com>",
            "Subject: Re: Lunch",
            'filename="m.pdf"',
        ]
        for line in expected:
            assert_that(raw, contains_string(line))
        assert_that("secret@" in raw, equal_to(False))
        assert_tags(self.world, outbox_id, "outbox")
        everyone = ["alice@example.com", "carol@example.com", "secret@example.com"]
        state = self.state(outbox_id)
        assert_that(state.recipients, contains_exactly(*everyone))

    def test_activate_once(self) -> None:
        """Activation sends once and remembers the SES ID."""
        outbox_id = self.reply()
        for _ in range(2):
            activate(self.context, outbox_id)
        sent = self.world.sender.deliveries()
        assert_that(len(sent), equal_to(1))
        assert_that(sent[0][1].tags, equal_to({"outbox_id": outbox_id}))
        state = self.state(outbox_id)
        assert_that(state.ses_message_id, equal_to("ses-1"))

    def test_confirm(self) -> None:
        """The Send event stores the final copy; later events only move forward."""
        outbox_id = self.reply()
        activate(self.context, outbox_id)
        mark_sent(self.context, outbox_id, "ses-1")
        stored = body(self.world, f"msgs/{outbox_id}/raw.eml").decode()
        assert_that(stored, contains_string(f"Message-ID: {SES_MESSAGE_ID}"))
        headers = read_json(self.world.store, f"msgs/{outbox_id}/headers.json")
        includes(headers, dict(message_id=SES_MESSAGE_ID))
        assert_tags(self.world, outbox_id, "sent")
        finish(self.context, outbox_id, Status.BOUNCED, "Permanent")
        assert_tags(self.world, outbox_id, "bounced", "sent")
        finish(self.context, outbox_id, Status.DELIVERED, "")
        final = self.state(outbox_id)
        assert_that(final.status, equal_to(Status.BOUNCED))

    def test_rejected(self) -> None:
        """A sandbox rejection is terminal and visible."""
        self.world.sender.fail_with(rejecting())
        outbox_id = post(self.world, "Hi")
        activate(self.context, outbox_id)
        rejected = self.state(outbox_id)
        assert_that(rejected.status, equal_to(Status.REJECTED))
        assert_that(rejected.error, contains_string("not verified"))
        assert_tags(self.world, outbox_id, "rejected")

    def assert_second_attempt_sends(self, retry: Callable[[], None]) -> None:
        """Run a retry and check that it sent the only message, on attempt two.

        Args:
            retry: Retries the only queued message.
        """
        retry()
        sent = self.world.sender.deliveries()
        assert_that(len(sent), equal_to(1))
        outbox_id = sent[0][1].tags["outbox_id"]
        assert_that(self.state(outbox_id).attempts, equal_to(2))

    def test_failed_send_releases_claim(self) -> None:
        """A send that fails gives the claim back, so a retry sends at once."""
        self.world.sender.fail_with(OSError("network"))
        outbox_id = post(self.world, "Re: Hi")
        assert_that(
            failure(lambda: activate(self.context, outbox_id)), instance_of(OSError)
        )
        released = self.state(outbox_id)
        assert_that(released.status, equal_to(Status.QUEUED))
        self.world.sender.recover()
        self.assert_second_attempt_sends(lambda: activate(self.context, outbox_id))

    def test_requeue(self) -> None:
        """A claim left behind (the function died mid-send) is sent again."""
        outbox_id = post(self.world, "Re: Hi")
        transition(self.context, outbox_id, claim(self.world.clock.time()))
        stale = self.state(outbox_id)
        assert_that(stale.status, equal_to(Status.SENDING))
        self.assert_second_attempt_sends(
            lambda: requeue(self.context, outbox_id, stale)
        )

    def test_replace_message_id_adds_header(self) -> None:
        """Rewriting a Message-ID adds one if the message had none."""
        rewritten = replace_message_id(b"Subject: x\r\n\r\nbody", "<n@x>")
        assert_that(rewritten, equal_to(b"Subject: x\r\nMessage-ID: <n@x>\r\n\r\nbody"))


class TestOutboxEdges(unittest.TestCase):
    """Races, declined changes and unusual attachments."""

    def setUp(self) -> None:
        """Create a world with a two-attachment message."""
        self.world = make_world(self)
        files = {"a.pdf": b"A", "b.pdf": b"B"}
        self.original = deliver(self.world, "f", sample_raw("Files", ".", files))

    def test_unknown_reference(self) -> None:
        """Only stored attachments and uploads can be attached."""
        message: EmailMessage[str, str] = EmailMessage()
        attempt = failure(lambda: attach("x", self.world.context, message))
        assert_that(attempt, instance_of(TypeError))

    def test_attachment_types(self) -> None:
        """Known attachments keep their type; unknown ones are octet-stream."""
        self.world.store.put(f"msgs/{self.original}/attachments/9-x.bin", b"X")
        refs = [
            AttachmentRef(message=self.original, name=name)
            for name in ["2-b.pdf", "9-x.bin"]
        ]
        draft = Draft(
            to=["a@example.com"],
            subject="RE: Files",
            body=".",
            in_reply_to=self.original,
            attachments=refs,
        )
        raw = queued_mime(self.world, enqueue(self.world.context, draft))
        expected = ["Subject: RE: Files", "application/pdf", "application/octet-stream"]
        for line in expected:
            assert_that(raw, contains_string(line))

    def test_races(self) -> None:
        """State changes retry lost races, and give up eventually."""
        first, second = post(self.world, "x"), post(self.world, "y")
        activate(racing(self.world, "outbox/", 1), first)
        state = read_state(self.world.context, first)
        assert_that(state.ses_message_id, equal_to("ses-1"))
        losing = racing(self.world, "outbox/", 99)
        assert_that(
            failure(lambda: activate(losing, second)), instance_of(PreconditionFailed)
        )

    def test_declined_changes(self) -> None:
        """Changes decline when the record has moved on."""
        state = OutboxState(status=Status.SENT, updated=1.0, attempts=1, recipients=())
        assert_that(record_ses_id("ses-1")(state), equal_to(None))
        assert_that(unclaim(2.0, 3.0)(state), equal_to(None))
