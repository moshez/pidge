"""Tests for the budget kill switch."""

import unittest

from hamcrest import assert_that, equal_to, has_length, instance_of

from ..handler import App, dispatch
from ..killswitch import is_trip
from ..toolkit import Services
from ..tools_read import READ_TOOLS
from .account import TOPIC, Account, sns_event
from .checks import failure
from .fakes import make_world, new_cache
from .flaky import GOOD

STOPPED = {"active_rule_set": None, "reserved_concurrency": 0}


class TestKillSwitch(unittest.TestCase):
    """The kill switch is recognized by its topic and is idempotent."""

    def setUp(self) -> None:
        """Wire an app to a fake account."""
        world = make_world(self)
        services = Services(context=world.context, cache=new_cache(world, "c"))
        self.account = Account.create()
        self.app = App(
            services=services,
            validator=GOOD,
            tools=READ_TOOLS,
            killswitch=self.account.killswitch(),
        )

    def test_trips_and_stays_tripped(self) -> None:
        """Tripping deactivates receiving, then stops the function; again is a no-op."""
        expected = {"receipt_rule_set": "deactivated", "reserved_concurrency": 0}
        for _ in range(2):
            assert_that(dispatch(self.app, sns_event(TOPIC)), equal_to(expected))
            assert_that(self.account.state, equal_to(STOPPED))
        operations = [name for name, _ in self.account.calls]
        assert_that(
            operations,
            equal_to(["SetActiveReceiptRuleSet", "PutFunctionConcurrency"] * 2),
        )
        assert_that(self.account.calls[0][1], equal_to({}))
        assert_that(
            self.account.calls[1][1],
            equal_to({"FunctionName": "pidge", "ReservedConcurrentExecutions": 0}),
        )
        assert_that(self.account.logged, has_length(6))

    def test_other_topics_do_not_trip(self) -> None:
        """An SNS record from any other topic is not the kill switch."""
        other = sns_event("arn:aws:sns:us-west-2:123456789012:something-else")
        assert_that(failure(lambda: dispatch(self.app, other)), instance_of(Exception))
        assert_that(self.account.calls, equal_to([]))

    def test_shapes(self) -> None:
        """Only SNS records count, and an unset topic never trips."""
        s3 = {"Records": [{"s3": {"object": {"key": "x"}}}]}
        assert_that(is_trip(self.account.killswitch(), s3), equal_to(False))
        unset = self.account.killswitch(topic="")
        assert_that(is_trip(unset, sns_event("")), equal_to(False))
        assert_that(is_trip(unset, sns_event(TOPIC)), equal_to(False))
