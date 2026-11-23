"""A fake Cognito user pool, shaped like the boto3 ``cognito-idp`` client."""

import dataclasses
from collections.abc import Mapping, Sequence

from ..clients import CognitoPoolClients, PoolClients

PAGE = 2


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class FakeCognito:
    """App clients by ID, plus the update requests received."""

    _clients: dict[str, dict[str, object]] = dataclasses.field(default_factory=dict)
    _requests: list[Mapping[str, object]] = dataclasses.field(default_factory=list)

    def add(self, client_id: str, flows: Sequence[str], kind: str = "DCR") -> None:
        """Register a client.

        Args:
            client_id: Its ID.
            flows: Its ExplicitAuthFlows.
            kind: ``DCR`` or ``REGULAR``.
        """
        self._clients[client_id] = {
            "UserPoolId": "pool",
            "ClientId": client_id,
            "ClientName": f"name-{client_id}",
            "ClientType": kind,
            "CreationDate": "2026-11-23",
            "ExplicitAuthFlows": list(flows),
            "CallbackURLs": ["https://claude.ai/callback"],
        }

    def list_user_pool_clients(self, **kwargs: object) -> Mapping[str, object]:
        """List clients of the requested type, PAGE at a time.

        Args:
            **kwargs: UserPoolId, ClientType, MaxResults and NextToken.

        Returns:
            One page.
        """
        matching = [
            {"ClientId": key}
            for key, value in sorted(self._clients.items())
            if value["ClientType"] == kwargs["ClientType"]
        ]
        start = int(str(kwargs.get("NextToken", "0")))
        page: dict[str, object] = {"UserPoolClients": matching[start : start + PAGE]}
        if start + PAGE < len(matching):
            page["NextToken"] = str(start + PAGE)
        return page

    def describe_user_pool_client(self, **kwargs: object) -> Mapping[str, object]:
        """Describe one client.

        Args:
            **kwargs: UserPoolId and ClientId.

        Returns:
            The description.
        """
        return {"UserPoolClient": dict(self._clients[str(kwargs["ClientId"])])}

    def update_user_pool_client(self, **kwargs: object) -> Mapping[str, object]:
        """Record and apply an update.

        Args:
            **kwargs: The request.

        Returns:
            An empty response.
        """
        self._requests.append(kwargs)
        self._clients[str(kwargs["ClientId"])].update(kwargs)
        return {}

    def flows(self) -> Mapping[str, object]:
        """Report each client's auth flows.

        Returns:
            ExplicitAuthFlows by client ID.
        """
        return {k: v["ExplicitAuthFlows"] for k, v in self._clients.items()}

    def updates(self) -> Sequence[Mapping[str, object]]:
        """Report the update requests received.

        Returns:
            The requests, in order.
        """
        return list(self._requests)

    def pool(self) -> PoolClients:
        """Wrap this fake the way production wraps boto3.

        Returns:
            The pool's clients.
        """
        return CognitoPoolClients.create(self, "pool")
