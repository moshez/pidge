"""Let Claude's dynamically registered OAuth clients offer email-code sign-in.

Cognito's managed login only offers passwordless choices (such as an
emailed code) to app clients that allow the ALLOW_USER_AUTH flow, and
Cognito registers DCR clients without it. A scheduled event runs
:func:`allow_email_codes` every minute, so a newly connected client can
sign in with a code within a minute of registering.
"""

import dataclasses
from collections.abc import Iterator, Mapping, Sequence
from typing import Protocol

from . import jsonvalue

USER_AUTH = "ALLOW_USER_AUTH"
# DescribeUserPoolClient returns these, and UpdateUserPoolClient rejects them.
READ_ONLY = frozenset(
    {
        "ClientSecret",
        "ClientSecretArn",
        "ClientType",
        "CreationDate",
        "LastModifiedDate",
    }
)


class CognitoClient(Protocol):
    """The subset of the boto3 ``cognito-idp`` client that this module uses."""

    def list_user_pool_clients(self, **kwargs: object) -> Mapping[str, object]:
        """List a pool's app clients.

        Args:
            **kwargs: Request parameters.

        Returns:
            The response.
        """

    def describe_user_pool_client(self, **kwargs: object) -> Mapping[str, object]:
        """Describe one app client.

        Args:
            **kwargs: Request parameters.

        Returns:
            The response.
        """

    def update_user_pool_client(self, **kwargs: object) -> Mapping[str, object]:
        """Replace an app client's settings.

        Args:
            **kwargs: Request parameters.

        Returns:
            The response.
        """


class PoolClients(Protocol):
    """The user pool's dynamically registered clients."""

    def registered(self) -> Iterator[Mapping[str, object]]:
        """Describe each DCR client.

        Returns:
            The DescribeUserPoolClient ``UserPoolClient`` of each.
        """

    def update(self, client: Mapping[str, object], flows: Sequence[str]) -> None:
        """Set a client's auth flows, keeping every other setting.

        Args:
            client: The client, as :meth:`registered` described it.
            flows: The new ExplicitAuthFlows.
        """


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class CognitoPoolClients:
    """The DCR clients of one Cognito user pool.

    Construct it with :meth:`create`.
    """

    _client: CognitoClient
    _pool_id: str

    @classmethod
    def create(cls, client: CognitoClient, pool_id: str) -> "CognitoPoolClients":
        """Wrap a boto3 ``cognito-idp`` client.

        Args:
            client: The client.
            pool_id: The user pool ID.

        Returns:
            The pool's clients.
        """
        return cls(_client=client, _pool_id=pool_id)

    def registered(self) -> Iterator[Mapping[str, object]]:
        """Describe each DCR client.

        Yields:
            The DescribeUserPoolClient ``UserPoolClient`` of each.
        """
        token: dict[str, object] = {}
        while True:
            page = self._client.list_user_pool_clients(
                UserPoolId=self._pool_id, ClientType="DCR", MaxResults=60, **token
            )
            for summary in jsonvalue.objects(page, "UserPoolClients"):
                described = self._client.describe_user_pool_client(
                    UserPoolId=self._pool_id,
                    ClientId=jsonvalue.get_str(summary, "ClientId"),
                )
                yield jsonvalue.as_dict(described["UserPoolClient"])
            if "NextToken" not in page:
                return
            token = {"NextToken": page["NextToken"]}

    def update(self, client: Mapping[str, object], flows: Sequence[str]) -> None:
        """Set a client's auth flows, keeping every other setting.

        Args:
            client: The client, as :meth:`registered` described it.
            flows: The new ExplicitAuthFlows.
        """
        request = {k: v for k, v in client.items() if k not in READ_ONLY}
        request["ExplicitAuthFlows"] = list(flows)
        self._client.update_user_pool_client(**request)


def allow_email_codes(pool: PoolClients) -> Sequence[str]:
    """Add ALLOW_USER_AUTH to each DCR client that lacks it.

    Args:
        pool: The pool's clients.

    Returns:
        The IDs of the clients changed.
    """
    changed = []
    for client in pool.registered():
        flows = jsonvalue.string_list(client, "ExplicitAuthFlows")
        if USER_AUTH in flows:
            continue
        pool.update(client, [*flows, USER_AUTH])
        changed.append(jsonvalue.get_str(client, "ClientId"))
    return changed
