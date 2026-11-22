"""Set up Pidge from scratch, or finish a setup that stopped part way.

    python3 infra/setup.py --email you@example.net

Run it from the repository root, signed in to AWS (administrator) and to the
GitHub CLI. It needs OpenTofu and boto3. Every step checks what already
exists first, so re-running it after a failure picks up where it stopped.

The only things it asks you to do are the ones no API can do for you:
create the Cloudflare token, click the SES verification link, merge the
pull request (or say yes and it merges), and connect your MCP client.
"""

import argparse
import email.parser
import email.policy
import getpass
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

import boto3

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "infra" / "scripts"))

from allow_email_codes import USER_AUTH, allow_email_codes  # noqa: E402
from enable_dcr import enable_dcr  # noqa: E402

RULE_SET = "pidge"
FUNCTION = "pidge"
POOL = "pidge"
PR = "1"
CLOUDFLARE = "https://api.cloudflare.com/client/v4"
POLL = 10


def say(text: str) -> None:
    print(f"\n==> {text}", flush=True)


def ok(text: str) -> None:
    print(f"    ok: {text}", flush=True)


def fail(text: str) -> None:
    print(
        f"\n!!  {text}\n    Fix that and run this script again; it resumes.",
        file=sys.stderr,
    )
    sys.exit(1)


def run(*command: str, stdin: str | None = None, quiet: bool = True) -> str:
    result = subprocess.run(
        command,
        input=stdin,
        text=True,
        stdout=subprocess.PIPE if quiet else None,
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        fail(f"`{' '.join(command[:3])} ...` failed (exit {result.returncode}).")
    return result.stdout or ""


def wait_for(check, what: str, timeout: float, every: float = POLL):
    """Poll ``check`` until it returns something truthy."""
    deadline = time.time() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.time() > deadline:
            fail(f"Gave up waiting for {what}.")
        time.sleep(every)


class Setup:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.repo = args.repo
        self.region = args.region
        self.domain = args.domain
        self.email = args.email
        session = boto3.Session(region_name=self.region)
        self.ses = session.client("ses")
        self.sesv2 = session.client("sesv2")
        self.s3 = session.client("s3")
        self.lam = session.client("lambda")
        self.cognito = session.client("cognito-idp")
        self.account = session.client("sts").get_caller_identity()["Account"]

    # -- GitHub ---------------------------------------------------------------

    def gh(self, *args: str, stdin: str | None = None, quiet: bool = True) -> str:
        return run("gh", *args, "--repo", self.repo, stdin=stdin, quiet=quiet)

    def secret_names(self) -> set[str]:
        listed = json.loads(self.gh("secret", "list", "--json", "name"))
        return {item["name"] for item in listed}

    # -- Steps ----------------------------------------------------------------

    def check_tools(self) -> None:
        say("Checking tools")
        for tool in ("gh", "tofu"):
            if not shutil.which(tool):
                fail(f"`{tool}` is not on PATH.")
        run("gh", "auth", "status")
        ok(f"AWS account {self.account}, region {self.region}, GitHub signed in")

    def check_rule_set(self) -> None:
        say("Checking for another active SES receipt rule set")
        active = self.ses.describe_active_receipt_rule_set().get("Metadata", {})
        name = active.get("Name")
        if name and name != RULE_SET:
            fail(
                f"SES rule set {name!r} is active in {self.region}. Deploying would "
                f"deactivate it. Move its rules into {RULE_SET!r} or pick another region."
            )
        ok("none besides Pidge's own" if name else "none active")

    def cloudflare(self, path: str, token: str) -> dict:
        request = urllib.request.Request(
            f"{CLOUDFLARE}{path}", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def cloudflare_token(self) -> None:
        say("Cloudflare API token")
        token = os.environ.get("CLOUDFLARE_API_TOKEN")
        if not token and "CLOUDFLARE_API_TOKEN" in self.secret_names():
            ok("already stored as a GitHub secret")
            return
        if not token:
            print(
                "    Create one at https://dash.cloudflare.com/profile/api-tokens\n"
                "    (Create Token > Create Custom Token), named pidge-dns, with:\n"
                "      Zone > DNS > Edit   and   Zone > Zone > Read\n"
                f"      Zone Resources: Include > Specific zone > {self.domain}"
            )
            token = getpass.getpass("    Paste the token (input hidden): ").strip()
        try:
            zones = self.cloudflare(
                f"/zones?name={urllib.parse.quote(self.domain)}", token
            )["result"]
        except OSError as error:
            fail(f"Cloudflare rejected the token: {error}")
        if not zones:
            fail(f"The token cannot see the zone {self.domain}.")
        zone = zones[0]["id"]
        records = self.cloudflare(
            f"/zones/{zone}/dns_records?type=MX&name={urllib.parse.quote(self.domain)}",
            token,
        )["result"]
        ours = f"inbound-smtp.{self.region}.amazonaws.com"
        others = [r["content"] for r in records if r["content"].rstrip(".") != ours]
        if others:
            fail(f"{self.domain} has MX records that would compete with SES: {others}")
        ok(f"token can edit {self.domain}, and no MX record competes with SES")
        self.gh("secret", "set", "CLOUDFLARE_API_TOKEN", stdin=token)
        ok("stored as the GitHub secret CLOUDFLARE_API_TOKEN")

    def bootstrap(self) -> dict[str, str]:
        say("Applying infra/bootstrap (state bucket, OIDC, deploy role, ECR)")
        repo_ids = json.loads(
            run(
                "gh",
                "api",
                f"repos/{self.repo}",
                "--jq",
                "{repo: .id, owner: .owner.id}",
            )
        )
        owner, name = self.repo.split("/")
        stack = "-chdir=infra/bootstrap"
        run("tofu", stack, "init", "-input=false")
        run(
            "tofu",
            stack,
            "apply",
            "-input=false",
            "-auto-approve",
            f"-var=state_bucket=pidge-tfstate-{self.account}",
            f"-var=region={self.region}",
            f"-var=github_owner={owner}",
            f"-var=github_owner_id={repo_ids['owner']}",
            f"-var=github_repository={name}",
            f"-var=github_repository_id={repo_ids['repo']}",
            quiet=False,
        )
        outputs = json.loads(run("tofu", stack, "output", "-json"))
        return {key: value["value"] for key, value in outputs.items()}

    def variables(self, bootstrap: dict[str, str]) -> None:
        say("Setting the GitHub repository variables")
        values = {
            "AWS_DEPLOY_ROLE_ARN": bootstrap["deploy_role_arn"],
            "TF_STATE_BUCKET": bootstrap["state_bucket"],
            "MAIL_DOMAIN": self.domain,
            "OWNER_EMAIL": self.email,
            "COGNITO_DOMAIN_PREFIX": self.args.cognito_prefix,
            "BUDGET_ALERT_EMAIL": self.args.budget_email,
            "BUDGET_LIMIT_USD": self.args.budget_limit,
        }
        for name, value in values.items():
            self.gh("variable", "set", name, "--body", value)
        ok(", ".join(values))

    def deploy(self) -> None:
        say(f"Merging pull request #{PR} and waiting for the deploy")
        pr = json.loads(self.gh("pr", "view", PR, "--json", "state,mergeCommit"))
        if pr["state"] == "OPEN":
            answer = input(f"    Merge pull request #{PR} now? [y/N] ").strip().lower()
            if answer == "y":
                self.gh("pr", "merge", PR, "--merge", quiet=False)
            else:
                print("    Waiting for you to merge it on GitHub...")
            pr = wait_for(
                lambda: (lambda p: p if p["state"] == "MERGED" else None)(
                    json.loads(self.gh("pr", "view", PR, "--json", "state,mergeCommit"))
                ),
                "the pull request to merge",
                timeout=24 * 3600,
            )
        elif pr["state"] != "MERGED":
            fail(f"Pull request #{PR} is {pr['state'].lower()}.")
        self.watch_deploy(pr["mergeCommit"]["oid"])

    def latest_deploy(self, sha: str | None) -> dict | None:
        runs = json.loads(
            self.gh(
                "run",
                "list",
                "--workflow",
                "deploy.yml",
                "--branch",
                "trunk",
                "--limit",
                "10",
                "--json",
                "databaseId,status,conclusion,headSha,event",
            )
        )
        for item in runs:
            if sha is None or item["headSha"] == sha:
                return item
        return None

    def watch_deploy(self, sha: str | None) -> None:
        latest = self.latest_deploy(sha)
        if latest and latest["conclusion"] == "success":
            ok("the deploy for this commit already succeeded")
            return
        found = wait_for(
            lambda: self.latest_deploy(sha), "the deploy run to start", 600
        )
        run_id = str(found["databaseId"])
        if found["status"] == "completed":
            print("    The last deploy failed; re-running it.")
            self.gh("run", "rerun", run_id)
            time.sleep(5)
        print(f"    Watching https://github.com/{self.repo}/actions/runs/{run_id}")
        result = subprocess.run(
            ["gh", "run", "watch", run_id, "--exit-status", "--repo", self.repo],
            cwd=ROOT,
        )
        if result.returncode != 0:
            self.gh("run", "view", run_id, "--log-failed", quiet=False)
            fail("The deploy failed (log above). Re-running the script re-runs it.")
        ok("deployed")

    def redeploy(self) -> None:
        self.gh("workflow", "run", "deploy.yml", "--ref", "trunk")
        time.sleep(5)
        found = wait_for(
            lambda: (lambda r: r if r and r["status"] != "completed" else None)(
                self.latest_deploy(None)
            ),
            "the deploy run to start",
            600,
        )
        self.watch_deploy(found["headSha"])

    def environment(self) -> dict[str, str]:
        config = self.lam.get_function_configuration(FunctionName=FUNCTION)
        return config["Environment"]["Variables"]

    def verify_owner(self) -> None:
        say(f"Verifying {self.email} with SES")

        def status() -> str:
            attributes = self.ses.get_identity_verification_attributes(
                Identities=[self.email, self.domain]
            )["VerificationAttributes"]
            return attributes.get(self.email, {}).get("VerificationStatus", "")

        if status() != "Success":
            print(
                f'    SES emailed {self.email} a link ("Amazon Web Services - Email '
                'Address Verification Request"). Click it; this waits until you do.'
            )
            resent = time.time()

            def verified() -> bool:
                nonlocal resent
                if time.time() - resent > 600:
                    self.ses.verify_email_identity(EmailAddress=self.email)
                    print("    Sent the verification email again.")
                    resent = time.time()
                return status() == "Success"

            wait_for(verified, "the SES verification link", timeout=24 * 3600)
        identity = self.sesv2.get_email_identity(EmailIdentity=self.domain)
        ok(
            f"{self.email} verified; {self.domain}: sending "
            f"{identity['VerifiedForSendingStatus']}, DKIM {identity['DkimAttributes']['Status']}, "
            f"MAIL FROM {identity.get('MailFromAttributes', {}).get('MailFromDomainStatus')}"
        )

    def production_access(self) -> None:
        say("SES production access")
        account = self.sesv2.get_account()
        if account.get("ProductionAccessEnabled"):
            ok("already enabled")
            return
        review = account.get("Details", {}).get("ReviewDetails", {}).get("Status")
        if review in ("PENDING", "GRANTED"):
            ok(f"request already filed ({review.lower()})")
            return
        self.sesv2.put_account_details(
            ProductionAccessEnabled=True,
            MailType="TRANSACTIONAL",
            WebsiteURL=f"https://{self.domain}",
            ContactLanguage="EN",
            AdditionalContactEmailAddresses=[self.email],
            UseCaseDescription=(
                f"Personal mailbox for one person on {self.domain}. Outgoing mail is "
                "individual replies and new messages written by its owner (a few a "
                "day). No marketing or bulk mail and no mailing lists. Bounces and "
                "complaints are recorded through a configuration set that publishes to "
                "EventBridge, and bounced messages are tagged and surfaced to the owner."
            ),
        )
        ok(
            "requested (AWS aims to review within 24 hours; until then mail only "
            "reaches verified addresses)"
        )

    def round_trip(self) -> None:
        say("Sending a message to the mailbox and checking it end to end")
        env = self.environment()
        bucket = env["BUCKET"]
        marker = uuid.uuid4().hex
        started = time.time() - 60
        sent = self.sesv2.send_email(
            FromEmailAddress=env["FROM_ADDRESS"],
            Destination={"ToAddresses": [f"setup-check@{self.domain}"]},
            Content={
                "Simple": {
                    "Subject": {"Data": f"Pidge setup check {marker}"},
                    "Body": {
                        "Text": {"Data": "Sent by infra/setup.py. Safe to ignore."}
                    },
                }
            },
        )["MessageId"]

        def arrived() -> tuple[str, str] | None:
            listed = self.s3.list_objects_v2(Bucket=bucket, Prefix="inbound/")
            for item in listed.get("Contents", []):
                if item["LastModified"].timestamp() < started:
                    continue
                head = self.s3.get_object(
                    Bucket=bucket, Key=item["Key"], Range="bytes=0-65535"
                )["Body"].read()
                headers = email.parser.BytesHeaderParser(
                    policy=email.policy.default
                ).parsebytes(head)
                if marker in str(headers.get("Subject", "")):
                    return item["Key"], str(headers.get("Message-ID", ""))
            return None

        key, message_id = wait_for(arrived, "the test message to arrive", 300)
        ok(f"received as s3://{bucket}/{key}")
        domain = message_id.strip("<>").partition("@")[2]
        local = message_id.strip("<>").partition("@")[0]
        if local != sent:
            print(f"    note: Message-ID {message_id} does not carry the SES ID {sent}")
        if domain and domain != env["MESSAGE_ID_DOMAIN"]:
            print(
                f"    SES uses @{domain} for Message-IDs here; updating and redeploying."
            )
            self.gh("variable", "set", "MESSAGE_ID_DOMAIN", "--body", domain)
            self.redeploy()
        else:
            ok(f"Message-ID domain is {env['MESSAGE_ID_DOMAIN']}")

        ses_id = key.removeprefix("inbound/")

        def ingested() -> str | None:
            try:
                body = self.s3.get_object(Bucket=bucket, Key=f"inbound-seen/{ses_id}")[
                    "Body"
                ]
            except self.s3.exceptions.NoSuchKey:
                return None
            ulid = body.read().decode().strip()
            listed = self.s3.list_objects_v2(
                Bucket=bucket, Prefix=f"msgs/{ulid}/raw.eml"
            )
            return ulid if listed.get("KeyCount") else None

        ulid = wait_for(ingested, "the message to be ingested", 300, every=5)
        ok(f"ingested as msgs/{ulid}/ (it is in your inbox, tagged inbox)")

    def pool(self) -> str:
        pools = self.cognito.list_user_pools(MaxResults=60)["UserPools"]
        matches = [p["Id"] for p in pools if p["Name"] == POOL]
        if not matches:
            fail("The Cognito user pool was not found; did the deploy finish?")
        return matches[0]

    def dcr(self, pool_id: str) -> None:
        say("Dynamic client registration")
        described = self.cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
        if described.get("DcrConfiguration", {}).get("EnableStatus") == "Enabled":
            ok("enabled")
            return
        servers = self.cognito.list_resource_servers(UserPoolId=pool_id, MaxResults=10)
        identifier = servers["ResourceServers"][0]["Identifier"]
        enable_dcr(self.cognito, pool_id, [f"{identifier}/full"])
        ok("enabled now")

    def dcr_clients(self, pool_id: str) -> list[dict]:
        clients = []
        pages = self.cognito.get_paginator("list_user_pool_clients").paginate(
            UserPoolId=pool_id, ClientType="DCR"
        )
        for page in pages:
            for summary in page["UserPoolClients"]:
                clients.append(
                    self.cognito.describe_user_pool_client(
                        UserPoolId=pool_id, ClientId=summary["ClientId"]
                    )["UserPoolClient"]
                )
        return clients

    def connect(self, pool_id: str) -> None:
        say("Connecting your MCP client")
        url = self.lam.get_function_url_config(FunctionName=FUNCTION)["FunctionUrl"]
        mcp = f"{url.rstrip('/')}/mcp"
        clients = self.dcr_clients(pool_id)
        if clients and all(
            USER_AUTH in c.get("ExplicitAuthFlows", []) for c in clients
        ):
            ok(f"{len(clients)} client(s) already registered and allowed email codes")
            print(f"    MCP URL: {mcp}")
            return
        print(
            f"    In Claude: Settings > Connectors > Add custom connector, paste\n"
            f"        {mcp}\n"
            "    click Add, then Connect. Close the sign-in page that opens; this waits\n"
            "    for the client to register."
        )
        known = {
            c["ClientId"]
            for c in clients
            if USER_AUTH in c.get("ExplicitAuthFlows", [])
        }
        wait_for(
            lambda: [
                c for c in self.dcr_clients(pool_id) if c["ClientId"] not in known
            ],
            "your MCP client to register",
            timeout=3600,
            every=5,
        )
        allow_email_codes(self.cognito, pool_id)
        print(
            f"    Now click Connect again and sign in as {self.email} with the code\n"
            '    emailed to you ("Pidge sign-in"). You should see six tools.'
        )

    def all(self) -> None:
        self.check_tools()
        self.check_rule_set()
        self.cloudflare_token()
        self.variables(self.bootstrap())
        self.deploy()
        self.verify_owner()
        self.production_access()
        self.round_trip()
        pool_id = self.pool()
        self.dcr(pool_id)
        self.connect(pool_id)
        say("Done. Problems, if any ever happen, show up in the mailbox tagged ops.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--email", required=True, help="your own address (the mailbox owner)"
    )
    parser.add_argument(
        "--budget-email",
        required=True,
        help="where budget alerts go; must be outside the mail domain",
    )
    parser.add_argument("--budget-limit", default="5", help="monthly budget in USD")
    parser.add_argument("--domain", default="ncolony.org")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--repo", default="moshez/pidge")
    parser.add_argument(
        "--cognito-prefix",
        default=None,
        help="managed-login domain prefix (default pidge-<domain label>)",
    )
    args = parser.parse_args()
    if args.budget_email.lower().endswith(f"@{args.domain.lower()}"):
        parser.error("--budget-email must be outside the mail domain")
    if args.cognito_prefix is None:
        args.cognito_prefix = f"pidge-{args.domain.split('.')[0]}"
    Setup(args).all()


if __name__ == "__main__":
    main()
