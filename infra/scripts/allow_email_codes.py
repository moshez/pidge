"""Let dynamically registered clients offer email-code sign-in.

Managed login only offers the passwordless choices (such as an emailed
code) to app clients that allow the ALLOW_USER_AUTH flow, and Cognito
registers DCR clients without it. Run this after an MCP client registers
(docs/SETUP.md, step 10):

    AWS_REGION=us-west-2 USER_POOL_ID=<pool id> python3 infra/scripts/allow_email_codes.py

It adds ALLOW_USER_AUTH to every DCR client that lacks it, keeping every
other client setting. Idempotent.
"""

import os

import boto3

from cognito_pool import update_members

USER_AUTH = "ALLOW_USER_AUTH"


def main() -> None:
    """Add ALLOW_USER_AUTH to each DCR client."""
    pool_id = os.environ["USER_POOL_ID"]
    client = boto3.client("cognito-idp", region_name=os.environ["AWS_REGION"])
    accepted = update_members(client, "UpdateUserPoolClient")
    pages = client.get_paginator("list_user_pool_clients").paginate(
        UserPoolId=pool_id, ClientType="DCR"
    )
    for page in pages:
        for summary in page["UserPoolClients"]:
            described = client.describe_user_pool_client(
                UserPoolId=pool_id, ClientId=summary["ClientId"]
            )["UserPoolClient"]
            flows = list(described.get("ExplicitAuthFlows", []))
            if USER_AUTH in flows:
                continue
            request = {k: v for k, v in described.items() if k in accepted}
            request["ExplicitAuthFlows"] = [*flows, USER_AUTH]
            client.update_user_pool_client(**request)
            print(f"Allowed email codes for {summary['ClientName']} ({summary['ClientId']})")


if __name__ == "__main__":
    main()
