"""The budget kill switch: stop receiving mail and stop the function.

AWS Budgets publishes to the ``budget-killswitch`` SNS topic when actual
spend passes the monthly limit. The function recognizes that SNS record by
its topic ARN and then:

1. deactivates the active SES receipt rule set, so SES stops accepting (and
   billing for) inbound mail;
2. sets its own reserved concurrency to 0, which stops every later
   invocation, the Function URL included.

Both calls set an end state rather than toggle one, so tripping the switch
twice is harmless. RECOVERY.md explains how to undo it.
"""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from . import jsonvalue

SNS_SOURCE = "aws:sns"


class ReceiptRules(Protocol):
    """Controls which SES receipt rule set is active."""

    def deactivate(self) -> None:
        """Leave no receipt rule set active."""


class Throttle(Protocol):
    """Controls the function's reserved concurrency."""

    def stop(self) -> None:
        """Set the function's reserved concurrency to 0."""


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class KillSwitch:
    """What the kill switch acts on.

    Attributes:
        topic_arn: The kill-switch SNS topic; only its records trip the switch.
        receipt_rules: Deactivates SES receiving.
        throttle: Stops the function.
        log: Records what was done.
    """

    topic_arn: str
    receipt_rules: ReceiptRules
    throttle: Throttle
    log: Callable[[str], None]


def sns_topics(event: Mapping[str, object]) -> Sequence[str]:
    """List the topic ARNs of an event's SNS records.

    Args:
        event: A Lambda event.

    Returns:
        One ARN per SNS record; empty if the event is not from SNS.
    """
    topics = []
    for record in jsonvalue.get_list(event, "Records"):
        fields = jsonvalue.as_dict(record)
        if jsonvalue.get_str(fields, "EventSource") == SNS_SOURCE:
            sns = jsonvalue.get_dict(fields, "Sns")
            topics.append(jsonvalue.get_str(sns, "TopicArn"))
    return topics


def is_trip(switch: KillSwitch, event: Mapping[str, object]) -> bool:
    """Tell whether an event is the kill-switch notification.

    Args:
        switch: The kill switch.
        event: A Lambda event.

    Returns:
        True if any of its SNS records came from the kill-switch topic.
    """
    return bool(switch.topic_arn) and switch.topic_arn in sns_topics(event)


def trip(switch: KillSwitch) -> Mapping[str, object]:
    """Stop inbound mail and the function. Idempotent.

    Args:
        switch: The kill switch.

    Returns:
        What was done.
    """
    switch.log("kill switch: budget exceeded; deactivating the SES receipt rule set")
    switch.receipt_rules.deactivate()
    switch.log("kill switch: SES receipt rule set deactivated")
    switch.throttle.stop()
    switch.log("kill switch: reserved concurrency set to 0; the function is stopped")
    return dict(receipt_rule_set="deactivated", reserved_concurrency=0)
