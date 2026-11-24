"""Tests for choosing the sender of an outgoing message."""

import email.policy
import unittest
from email import message_from_bytes

from hamcrest import assert_that, equal_to, starts_with

from ..compose import Draft
from ..outbox import enqueue
from ..tools_write import sender_header
from .checks import failure
from .fakes import SETTINGS, body, make_world


def queued_from(case: unittest.TestCase, sender: str) -> str:
    """Queue a message with a sender and read its From header.

    Args:
        case: The test case.
        sender: The ``Draft.sender`` value.

    Returns:
        The From header of the queued MIME.
    """
    world = make_world(case)
    draft = Draft(to=["alice@example.com"], subject="Hi", body=".", sender=sender)
    outbox_id = enqueue(world.context, draft)
    raw = body(world, f"outbox/{outbox_id}/message.eml")
    return str(message_from_bytes(raw, policy=email.policy.default)["From"])


class TestSender(unittest.TestCase):
    """The ``from`` argument of ``send_email``."""

    def test_default(self) -> None:
        """No sender means the configured address."""
        assert_that(sender_header(SETTINGS, ""), equal_to(""))
        assert_that(queued_from(self, ""), equal_to("Me <me@example.org>"))

    def test_custom(self) -> None:
        """Any local part under the mail domain can send."""
        header = sender_header(SETTINGS, "Daily summary <summary@example.org>")
        assert_that(header, equal_to("Daily summary <summary@example.org>"))
        assert_that(queued_from(self, header), equal_to(header))

    def test_bare(self) -> None:
        """A bare address keeps the configured display name."""
        header = sender_header(SETTINGS, "summary@EXAMPLE.org")
        assert_that(header, equal_to("Me <summary@EXAMPLE.org>"))

    def test_rejected(self) -> None:
        """Other domains, non-addresses, lists and line breaks are refused."""
        cases = {
            "a@evil.org": "from must be one address under example.org",
            "nobody": "from must be one address under example.org",
            "a@example.org, b@example.org": "from must be one address",
            "a@sub.example.org": "from must be one address",
            "X\r\nBcc: v <a@example.org>": "from must not contain line breaks",
            "a@example.org\n": "from must not contain line breaks",
        }
        for requested, message in cases.items():
            error = failure(lambda: sender_header(SETTINGS, requested))
            assert_that(str(error), starts_with(message))
