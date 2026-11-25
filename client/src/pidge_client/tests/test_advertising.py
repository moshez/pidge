"""pidge's server points Claude at pidge_client, with an example that runs."""

import contextlib
import io
import unittest

from hamcrest import assert_that, contains_string, string_contains_in_order

from pidge.mcp import CLIENT_EXAMPLE, INSTRUCTIONS, REPOSITORY
from pidge.sessions import GET_SESSION_TOKEN

from .mailbox import TOKEN, BASE, dated, empty_mailbox, http_server, receive


class TestAdvertising(unittest.TestCase):
    """The initialize instructions and the token tool's description."""

    def test_instructions(self) -> None:
        """The instructions name the client, the repository and the example."""
        for text in ("pidge_client", REPOSITORY, "client/src", CLIENT_EXAMPLE):
            assert_that(INSTRUCTIONS, contains_string(text))

    def test_token_description(self) -> None:
        """The token tool says what the token is for and where the client is."""
        for text in ("pidge_client", REPOSITORY, "client/src"):
            assert_that(GET_SESSION_TOKEN.description, contains_string(text))

    def test_example_runs(self) -> None:
        """The example, run as written over HTTP, prints the inbox."""
        mailbox = empty_mailbox(self)
        receive(mailbox, "a", dated("First in the inbox", BASE))
        receive(mailbox, "b", dated("Second in the inbox", BASE + 60))
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            base = stack.enter_context(http_server(mailbox.transport))
            stack.enter_context(contextlib.redirect_stdout(out))
            source = CLIENT_EXAMPLE.replace("<endpoint>", f"{base}/mcp")
            code = compile(source.replace("<token>", TOKEN), "<example>", "exec")
            exec(code, {})  # noqa: SLD103
        printed = string_contains_in_order("2 messages", "Second in the inbox")
        assert_that(out.getvalue(), printed)
