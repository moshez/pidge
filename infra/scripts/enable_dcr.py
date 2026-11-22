"""Turn on dynamic client registration (RFC 7591) for the user pool.

Run by OpenTofu (infra/main/cognito.tf) because the AWS provider cannot set
it yet. Environment: AWS_REGION, USER_POOL_ID, DCR_SCOPES (space-separated
custom scopes registered clients may request).

Open registration with the authorization-code grant only: anyone can
register a client, but nobody gets a token without signing in as the pool's
only user. Idempotent.
"""

import os

import boto3

from cognito_pool import pool_update


def enable_dcr(client, pool_id: str, scopes: list[str]) -> None:
    """Enable DCR on a pool, keeping every other pool setting.

    Args:
        client: A boto3 cognito-idp client.
        pool_id: The user pool ID.
        scopes: The custom scopes registered clients may request.
    """
    described = client.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    request = pool_update(client, described)
    request["DcrConfiguration"] = {
        "EnableStatus": "Enabled",
        "OpenDcrEnabled": True,
        "OpenDcrGrantTypes": ["code"],
        "AuthenticatedGrantTypes": [],
        "AllowedScopes": scopes,
    }
    client.update_user_pool(UserPoolId=pool_id, **request)
    print(f"Dynamic client registration enabled on {pool_id} for {scopes}")


def main() -> None:
    """Enable DCR on the pool named by the environment."""
    client = boto3.client("cognito-idp", region_name=os.environ["AWS_REGION"])
    enable_dcr(client, os.environ["USER_POOL_ID"], os.environ["DCR_SCOPES"].split())


if __name__ == "__main__":
    main()
