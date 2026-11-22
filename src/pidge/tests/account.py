"""A fake AWS account for the kill switch: SES receiving and Lambda concurrency."""

import dataclasses
from collections.abc import Mapping

from ..awsclients import LambdaThrottle, SesReceiptRules
from ..killswitch import KillSwitch

TOPIC = "arn:aws:sns:us-west-2:123456789012:budget-killswitch"


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Account:
    """The state the kill switch changes, behind boto3-shaped clients.

    Construct it with :meth:`create`.

    Attributes:
        state: ``active_rule_set`` and ``reserved_concurrency``.
        calls: Every request, as ``(operation, parameters)``.
        logged: Lines the kill switch logged.
    """

    state: dict[str, object]  # noqa: SLD803
    calls: list[tuple[str, Mapping[str, object]]]  # noqa: SLD803
    logged: list[str]  # noqa: SLD803

    @classmethod
    def create(cls) -> "Account":
        """Create an account with the pidge rule set active and no concurrency limit.

        Returns:
            The account.
        """
        state: dict[str, object] = dict(
            active_rule_set="pidge", reserved_concurrency=None
        )
        return cls(state=state, calls=[], logged=[])

    def apply(
        self, operation: str, request: Mapping[str, object], **changes: object
    ) -> None:
        """Record a request and the state it leaves behind.

        Args:
            operation: The API operation.
            request: Its parameters.
            **changes: The state it sets.
        """
        self.calls.append((operation, request))
        self.state.update(changes)

    def set_active_receipt_rule_set(self, **kwargs: object) -> Mapping[str, object]:
        """SES SetActiveReceiptRuleSet: no name leaves none active.

        Args:
            **kwargs: The request.

        Returns:
            An empty response.
        """
        changes = dict(active_rule_set=kwargs.get("RuleSetName"))
        self.apply("SetActiveReceiptRuleSet", kwargs, **changes)
        return {}

    def put_function_concurrency(self, **kwargs: object) -> Mapping[str, object]:
        """Lambda PutFunctionConcurrency.

        Args:
            **kwargs: The request.

        Returns:
            The new setting.
        """
        self.apply(
            "PutFunctionConcurrency",
            kwargs,
            reserved_concurrency=kwargs["ReservedConcurrentExecutions"],
        )
        return {"ReservedConcurrentExecutions": self.state["reserved_concurrency"]}

    def killswitch(self, topic: str = TOPIC) -> KillSwitch:
        """Wire a kill switch to this account through the real adapters.

        Args:
            topic: The kill-switch topic ARN.

        Returns:
            The kill switch.
        """
        return KillSwitch(
            topic_arn=topic,
            receipt_rules=SesReceiptRules.create(self),
            throttle=LambdaThrottle.create(self, "pidge"),
            log=self.logged.append,
        )


def sns_event(topic: str) -> Mapping[str, object]:
    """Build an SNS notification as Lambda receives it.

    Args:
        topic: The topic ARN.

    Returns:
        The event.
    """
    message = '{"budgetName": "pidge-monthly"}'
    record = {
        "EventSource": "aws:sns",
        "EventVersion": "1.0",
        "Sns": {"Type": "Notification", "TopicArn": topic, "Message": message},
    }
    return {"Records": [record]}
