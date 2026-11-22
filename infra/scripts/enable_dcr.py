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


def main() -> None:
    """Enable DCR, keeping every other pool setting."""
    pool_id = os.environ["USER_POOL_ID"]
    scopes = os.environ["DCR_SCOPES"].split()
    client = boto3.client("cognito-idp", region_name=os.environ["AWS_REGION"])
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


if __name__ == "__main__":
    main()
