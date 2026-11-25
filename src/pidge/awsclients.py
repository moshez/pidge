"""Adapters from boto3 clients to this package's ports."""

import dataclasses
import json
from collections.abc import Callable, Mapping
from typing import Protocol

from . import jsonvalue
from .ports import Envelope, SendRejected
from .s3store import URLSigner

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


class ConcurrencyClient(Protocol):
    """The subset of the boto3 ``lambda`` client that the throttle uses."""

    def put_function_concurrency(self, **kwargs: object) -> Mapping[str, object]:
        """Set a function's reserved concurrency.

        Args:
            **kwargs: Request parameters.

        Returns:
            The response.
        """


class SSMClient(Protocol):
    """The subset of the boto3 ``ssm`` client that this module uses."""

    def get_parameter(self, **kwargs: object) -> Mapping[str, object]:
        """Read a parameter.

        Args:
            **kwargs: Request parameters.

        Returns:
            The response.
        """


class SESClient(Protocol):
    """The subset of the boto3 ``ses`` (v1) client that this module uses."""

    def set_active_receipt_rule_set(self, **kwargs: object) -> Mapping[str, object]:
        """Set (or, with no name, clear) the active receipt rule set.

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


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class SesReceiptRules:
    """Turns SES receiving off.

    Construct it with :meth:`create`.
    """

    _client: SESClient

    @classmethod
    def create(cls, client: SESClient) -> "SesReceiptRules":
        """Wrap a boto3 ``ses`` client.

        Args:
            client: The client.

        Returns:
            The receipt rule control.
        """
        return cls(_client=client)

    def deactivate(self) -> None:
        """Leave no receipt rule set active (SetActiveReceiptRuleSet, no name)."""
        self._client.set_active_receipt_rule_set()


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class LambdaThrottle:
    """Stops a function by setting its reserved concurrency to 0.

    Construct it with :meth:`create`.
    """

    _client: ConcurrencyClient
    _function: str

    @classmethod
    def create(cls, client: ConcurrencyClient, function: str) -> "LambdaThrottle":
        """Wrap a boto3 ``lambda`` client.

        Args:
            client: The client.
            function: The function name.

        Returns:
            The throttle.
        """
        return cls(_client=client, _function=function)

    def stop(self) -> None:
        """Set the function's reserved concurrency to 0."""
        self._client.put_function_concurrency(
            FunctionName=self._function, ReservedConcurrentExecutions=0
        )


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class SsmSecret:
    """A SecureString parameter, read on first use and then cached.

    Construct it with :meth:`create`.
    """

    _client: SSMClient
    _name: str
    _cache: list[bytes]

    @classmethod
    def create(cls, client: SSMClient, name: str) -> "SsmSecret":
        """Wrap a boto3 ``ssm`` client.

        Args:
            client: The client.
            name: The parameter name.

        Returns:
            The secret.
        """
        return cls(_client=client, _name=name, _cache=[])

    def value(self) -> bytes:
        """Return the parameter's decrypted value.

        Returns:
            The value, UTF-8 encoded.
        """
        if not self._cache:
            response = self._client.get_parameter(Name=self._name, WithDecryption=True)
            parameter = jsonvalue.as_dict(response["Parameter"])
            self._cache.append(jsonvalue.get_str(parameter, "Value").encode("utf-8"))
        return self._cache[0]


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class KeyedSigner:
    """Presigns S3 URLs with a fixed IAM access key, built on first use.

    A presigned URL stops working when the credentials that signed it
    expire, and the Lambda role's temporary credentials can expire within
    the hour. A long-term key lets a URL last its whole requested lifetime.

    Construct it with :meth:`create`.
    """

    _key_id: str
    _secret: Callable[[], bytes]
    _factory: Callable[[str, str], URLSigner]
    _cache: list[URLSigner]

    @classmethod
    def create(
        cls,
        key_id: str,
        secret: Callable[[], bytes],
        factory: Callable[[str, str], URLSigner],
    ) -> "KeyedSigner":
        """Describe the signer without contacting AWS.

        Args:
            key_id: The access key ID.
            secret: Returns the secret access key.
            factory: Builds an S3 client from a key ID and secret.

        Returns:
            The signer.
        """
        return cls(_key_id=key_id, _secret=secret, _factory=factory, _cache=[])

    def generate_presigned_url(self, *args: object, **kwargs: object) -> str:
        """Create a presigned URL.

        Args:
            *args: Positional parameters.
            **kwargs: Request parameters.

        Returns:
            The URL.
        """
        if not self._cache:
            secret = self._secret().decode("utf-8")
            self._cache.append(self._factory(self._key_id, secret))
        return self._cache[0].generate_presigned_url(*args, **kwargs)
