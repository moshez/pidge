"""Adapters from boto3 clients to this package's ports."""

import dataclasses
import json
from collections.abc import Mapping
from typing import Protocol

from . import jsonvalue
from .ports import Envelope, SendRejected

REJECTION_CODES = frozenset(
    {
        "MessageRejected",
        "MailFromDomainNotVerifiedException",
        "AccountSuspendedException",
        "SendingPausedException",
        "BadRequestException",
        "NotFoundException",
    }
)


class SESv2Client(Protocol):
    """The subset of the boto3 ``sesv2`` client that this module uses."""

    def send_email(self, **kwargs: object) -> Mapping[str, object]:
        """Send an email.

        Args:
            **kwargs: Request parameters.

        Returns:
            The response.
        """


class LambdaClient(Protocol):
    """The subset of the boto3 ``lambda`` client that this module uses."""

    def invoke(self, **kwargs: object) -> Mapping[str, object]:
        """Invoke a function.

        Args:
            **kwargs: Request parameters.

        Returns:
            The response.
        """


def error_code(error: Exception) -> str:
    """Read the AWS error code from a botocore ClientError.

    Args:
        error: The exception.

    Returns:
        The error code, or an empty string.
    """
    return jsonvalue.get_str(jsonvalue.error_details(error), "Code")


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class SesSender:
    """Sends raw MIME with SES v2.

    Construct it with :meth:`create`.
    """

    _client: SESv2Client
    _configuration_set: str

    @classmethod
    def create(cls, client: SESv2Client, configuration_set: str) -> "SesSender":
        """Wrap a boto3 ``sesv2`` client.

        Args:
            client: The client.
            configuration_set: The configuration set that publishes events.

        Returns:
            The sender.
        """
        return cls(_client=client, _configuration_set=configuration_set)

    def send(self, raw: bytes, envelope: Envelope) -> str:
        """Send a message.

        Args:
            raw: The MIME bytes.
            envelope: Recipients and tags.

        Returns:
            The SES message ID.

        Raises:
            SendRejected: If SES refuses the message permanently.
            Exception: Any other client error, unchanged.
        """
        tags = [dict(Name=name, Value=value) for name, value in envelope.tags.items()]
        try:
            response = self._client.send_email(
                Content=dict(Raw=dict(Data=raw)),
                Destination=dict(ToAddresses=list(envelope.recipients)),
                ConfigurationSetName=self._configuration_set,
                EmailTags=tags,
            )
        except Exception as error:
            if error_code(error) in REJECTION_CODES:
                raise SendRejected(f"{error_code(error)}: {error}") from error
            raise
        return jsonvalue.as_str(response["MessageId"])


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class LambdaInvoker:
    """Invokes a Lambda function asynchronously.

    Construct it with :meth:`create`.
    """

    _client: LambdaClient
    _function: str

    @classmethod
    def create(cls, client: LambdaClient, function: str) -> "LambdaInvoker":
        """Wrap a boto3 ``lambda`` client.

        Args:
            client: The client.
            function: The function name.

        Returns:
            The invoker.
        """
        return cls(_client=client, _function=function)

    def invoke(self, payload: Mapping[str, object]) -> None:
        """Queue an asynchronous invocation.

        Args:
            payload: The event to send.
        """
        self._client.invoke(
            FunctionName=self._function,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
