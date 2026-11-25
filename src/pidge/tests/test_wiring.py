"""Tests for the event dispatcher and the AWS adapters."""

import dataclasses
import json
import unittest
from collections.abc import Mapping, Sequence

from hamcrest import (
    assert_that,
    equal_to,
    has_item,
    has_length,
    instance_of,
)

from .. import jsonvalue
from ..awsclients import LambdaInvoker, SesSender, SsmSecret
from ..handler import App, dispatch
from ..outbox import read_state
from ..ports import Envelope, SendRejected
from ..toolkit import Services
from ..tools_read import READ_TOOLS
from .account import Account
from .checks import failure, includes
from .cognito import FakeCognito
from .flaky import GOOD, FlakyStore, client_error
from .sessionkey import SESSION_KEY
from .fakes import (
    PLAIN,
    World,
    drop,
    keys,
    make_world,
    new_cache,
    post,
)


def s3_event(*keys: str) -> Mapping[str, object]:
    """Build an S3 notification.

    Args:
        *keys: The created keys.

    Returns:
        The event.
    """
    return {"Records": [{"s3": {"object": {"key": key}}} for key in keys]}


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class FakeAWS:
    """Records calls to fake SES and Lambda clients.

    Construct it with :meth:`create`.
    """

    _calls: list[Mapping[str, object]]
    _errors: list[Exception]

    @classmethod
    def create(cls, *errors: Exception) -> "FakeAWS":
        """Create a fake.

        Args:
            *errors: Raised by SendEmail, at most one.

        Returns:
            The fake.
        """
        return cls(_calls=[], _errors=list(errors))

    def recorded(self) -> Sequence[Mapping[str, object]]:
        """Return the recorded requests.

        Returns:
            The requests.
        """
        return list(self._calls)

    def send_email(self, **kwargs: object) -> Mapping[str, object]:
        """Pretend to send.

        Args:
            **kwargs: The request.

        Returns:
            A response.

        Raises:
            error: The configured error, if any.
        """
        self._calls.append(kwargs)
        for error in self._errors:
            raise error
        return {"MessageId": "ses-9"}

    def get_parameter(self, **kwargs: object) -> Mapping[str, object]:
        """Pretend to read a parameter.

        Args:
            **kwargs: The request.

        Returns:
            A response.
        """
        self._calls.append(kwargs)
        return {"Parameter": {"Value": "sekrit"}}

    def invoke(self, **kwargs: object) -> Mapping[str, object]:
        """Pretend to invoke.

        Args:
            **kwargs: The request.

        Returns:
            A response.
        """
        self._calls.append(kwargs)
        return {}


class TestDispatch(unittest.TestCase):
    """Each event shape reaches its flow."""

    def setUp(self) -> None:
        """Build an app."""
        self.world: World = make_world(self)
        self.cognito = FakeCognito()
        cache = new_cache(self.world, "cache")
        services = Services(
            context=self.world.context, cache=cache, session_key=SESSION_KEY
        )
        self.app = App(
            services=services,
            validator=GOOD,
            tools=READ_TOOLS,
            killswitch=Account.create().killswitch(),
            pool=self.cognito.pool(),
        )

    def test_s3(self) -> None:
        """Inbound mail is ingested and queued mail is sent."""
        drop(self.world, "a+b", PLAIN)
        outbox_id = post(self.world, "s")
        names = [
            "inbound/a%2Bb",
            f"outbox/{outbox_id}/message.eml",
            f"outbox/{outbox_id}/state.json",
            "other/thing",
        ]
        assert_that(
            jsonvalue.as_list(dispatch(self.app, s3_event(*names))), has_length(4)
        )
        assert_that(keys(self.world), has_item("inbound-done/a+b"))
        assert_that(
            read_state(self.world.context, outbox_id).ses_message_id, equal_to("ses-1")
        )

    def test_other_shapes(self) -> None:
        """SES events, HTTP requests and actions; anything else is an error."""
        ses = {"source": "aws.ses", "detail-type": "Email Sent", "detail": {"mail": {}}}
        assert_that(dispatch(self.app, ses), equal_to(False))
        http = {
            "rawPath": "/nowhere",
            "requestContext": {"domainName": "h", "http": {"method": "GET"}},
        }
        includes(dispatch(self.app, http), dict(statusCode=404))
        assert_that(dispatch(self.app, {"action": "index"}), equal_to({"indexed": 0}))
        assert_that(
            dispatch(self.app, {"action": "reconcile"}), equal_to({"ops_messages": []})
        )
        self.cognito.add("new", [])
        clients = dispatch(self.app, {"action": "clients"})
        assert_that(clients, equal_to({"email_codes_allowed": ["new"]}))
        assert_that(failure(lambda: dispatch(self.app, {})), instance_of(ValueError))

    def test_reconcile_replays_through_dispatch(self) -> None:
        """Replayed events go back through the dispatcher."""
        drop(self.world, "r", PLAIN)
        record = {"requestPayload": s3_event("inbound/r")}
        self.world.store.put("aws/lambda/async/f/1", json.dumps(record).encode())
        dispatch(self.app, {"action": "reconcile"})
        done = keys(self.world)
        assert_that(done, has_item("inbound-done/r"))
        flaky = FlakyStore.create(self.world.store, "x", 0)
        assert_that(
            flaky.presign("inbound/r", "r.eml"),
            equal_to("memory:inbound/r?filename=r.eml"),
        )


class TestAdapters(unittest.TestCase):
    """The boto3 adapters translate requests and errors."""

    def test_send(self) -> None:
        """Sends carry the envelope and tags."""
        client = FakeAWS.create()
        sender = SesSender.create(client, "pidge")
        envelope = Envelope(recipients=["a@example.com"], tags={"outbox_id": "X"})
        assert_that(sender.send(b"raw", envelope), equal_to("ses-9"))
        includes(
            client.recorded()[0],
            dict(
                ConfigurationSetName="pidge",
                EmailTags=[{"Name": "outbox_id", "Value": "X"}],
            ),
        )

    def test_send_errors(self) -> None:
        """Permanent refusals become SendRejected; others pass through."""
        envelope = Envelope(recipients=["a@example.com"], tags={})
        outcomes = [("MessageRejected", SendRejected), ("Throttling", Exception)]
        for code, raised in outcomes:
            client = FakeAWS.create(client_error(code, 400))
            send = SesSender.create(client, "pidge").send
            assert_that(failure(lambda: send(b"", envelope)), instance_of(raised))

    def test_invoke(self) -> None:
        """Invocations are asynchronous."""
        client = FakeAWS.create()
        LambdaInvoker.create(client, "pidge").invoke({"action": "index"})
        includes(
            client.recorded()[0], dict(FunctionName="pidge", InvocationType="Event")
        )

    def test_secret(self) -> None:
        """The parameter is decrypted once, then cached."""
        client = FakeAWS.create()
        secret = SsmSecret.create(client, "/pidge/key")
        assert_that([secret.value(), secret.value()], equal_to([b"sekrit"] * 2))
        assert_that(
            client.recorded(), equal_to([dict(Name="/pidge/key", WithDecryption=True)])
        )
