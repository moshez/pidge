"""Finish a deploy: request SES production access, then check mail end to end.

Run by the deploy workflow after the main stack is applied, with the deploy
role's credentials. Environment: AWS_REGION, MAIL_DOMAIN. The owner's
address comes from the function's configuration, so it never appears in the
(public) Actions log.

1. Files the SES production access request if it isn't filed yet.
2. Once per mailbox, sends a message to setup-check@<domain> and waits until
   it is received and ingested, checking that the Message-ID domain the
   function expects matches the one SES really uses. When that passes it
   writes setup/round-trip-ok to the mail bucket, so later deploys skip it.

Any failure exits non-zero with a message saying what to look at.
"""

import email.parser
import email.policy
import os
import sys
import time
import uuid

import boto3

FUNCTION = "pidge"
MARKER = "setup/round-trip-ok"
TIMEOUT = 300


def production_access(sesv2, domain: str, owner: str) -> None:
    """File the SES production access request once.

    Args:
        sesv2: A boto3 sesv2 client.
        domain: The mail domain.
        owner: The owner's address.
    """
    account = sesv2.get_account()
    if account.get("ProductionAccessEnabled"):
        print("SES production access: enabled")
        return
    review = account.get("Details", {}).get("ReviewDetails", {}).get("Status")
    if review in ("PENDING", "GRANTED"):
        print(f"SES production access: request {review.lower()}")
        return
    sesv2.put_account_details(
        ProductionAccessEnabled=True,
        MailType="TRANSACTIONAL",
        WebsiteURL=f"https://{domain}",
        ContactLanguage="EN",
        AdditionalContactEmailAddresses=[owner],
        UseCaseDescription=(
            f"Personal mailbox for one person on {domain}. Outgoing mail is "
            "individual replies and new messages written by its owner (a few a "
            "day). No marketing or bulk mail and no mailing lists. Bounces and "
            "complaints are recorded through a configuration set that publishes to "
            "EventBridge, and bounced messages are tagged and surfaced to the owner."
        ),
    )
    print("SES production access: requested (AWS aims to review within 24 hours)")


def wait_for(check, what: str, problem: str):
    """Poll until check returns something truthy.

    Args:
        check: Returns the result, or something falsy to keep waiting.
        what: What is being waited for.
        problem: What to look at if it never comes.

    Returns:
        The result.
    """
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(5)
    sys.exit(f"Timed out after {TIMEOUT}s waiting for {what}. {problem}")


def exists(s3, bucket: str, key: str) -> bool:
    """Check whether an object exists.

    Args:
        s3: A boto3 s3 client.
        bucket: The bucket.
        key: The key.

    Returns:
        True if it does.
    """
    return s3.list_objects_v2(Bucket=bucket, Prefix=key).get("KeyCount", 0) > 0


def round_trip(sesv2, s3, lam, domain: str, region: str) -> None:
    """Send a message to the mailbox and wait until it is ingested.

    Args:
        sesv2: A boto3 sesv2 client.
        s3: A boto3 s3 client.
        lam: A boto3 lambda client.
        domain: The mail domain.
        region: The AWS region.
    """
    env = lam.get_function_configuration(FunctionName=FUNCTION)["Environment"][
        "Variables"
    ]
    bucket = env["BUCKET"]
    if exists(s3, bucket, MARKER):
        print("Mail round trip: checked on an earlier deploy")
        return
    marker = uuid.uuid4().hex
    started = time.time() - 60
    sent = sesv2.send_email(
        FromEmailAddress=env["FROM_ADDRESS"],
        Destination={"ToAddresses": [f"setup-check@{domain}"]},
        Content={
            "Simple": {
                "Subject": {"Data": f"Pidge setup check {marker}"},
                "Body": {"Text": {"Data": "Sent by the deploy. Safe to ignore."}},
            }
        },
    )["MessageId"]

    def arrived():
        listed = s3.list_objects_v2(Bucket=bucket, Prefix="inbound/")
        for item in listed.get("Contents", []):
            if item["LastModified"].timestamp() < started:
                continue
            head = s3.get_object(Bucket=bucket, Key=item["Key"], Range="bytes=0-65535")
            headers = email.parser.BytesHeaderParser(
                policy=email.policy.default
            ).parsebytes(head["Body"].read())
            if marker in str(headers.get("Subject", "")):
                return item["Key"], str(headers.get("Message-ID", ""))
        return None

    key, message_id = wait_for(
        arrived,
        "the test message to arrive",
        f"Check that the MX record for {domain} points at "
        f"inbound-smtp.{region}.amazonaws.com and that the pidge receipt rule "
        "set is active.",
    )
    print(f"Mail round trip: received as s3://{bucket}/{key}")
    local, _, used = message_id.strip("<>").partition("@")
    if local != sent:
        print(f"note: Message-ID {message_id} does not carry the SES ID {sent}")
    if used and used != env["MESSAGE_ID_DOMAIN"]:
        sys.exit(
            f"SES uses @{used} for Message-IDs, but the function expects "
            f"@{env['MESSAGE_ID_DOMAIN']}. Set the repository variable "
            f"MESSAGE_ID_DOMAIN to {used} and deploy again."
        )
    ses_id = key.removeprefix("inbound/")

    def ingested():
        if not exists(s3, bucket, f"inbound-seen/{ses_id}"):
            return None
        body = s3.get_object(Bucket=bucket, Key=f"inbound-seen/{ses_id}")["Body"]
        ulid = body.read().decode().strip()
        return ulid if exists(s3, bucket, f"msgs/{ulid}/raw.eml") else None

    ulid = wait_for(
        ingested,
        "the message to be ingested",
        f"Check the function's log: aws logs tail /aws/lambda/{FUNCTION} --since 15m",
    )
    s3.put_object(Bucket=bucket, Key=MARKER, Body=ulid.encode())
    print(f"Mail round trip: ingested as msgs/{ulid}/")


def main() -> None:
    """Run both checks for the deployment named by the environment."""
    region = os.environ["AWS_REGION"]
    domain = os.environ["MAIL_DOMAIN"]
    sesv2 = boto3.client("sesv2", region_name=region)
    lam = boto3.client("lambda", region_name=region)
    env = lam.get_function_configuration(FunctionName=FUNCTION)["Environment"]
    production_access(sesv2, domain, env["Variables"]["OWNER_ADDRESS"])
    round_trip(sesv2, boto3.client("s3", region_name=region), lam, domain, region)


if __name__ == "__main__":
    main()
