"""Set up Pidge from scratch, or finish a setup that stopped part way.

    python3 infra/setup.py --email you@example.net

Run it from the repository root. It first checks for everything it needs
(a current boto3, OpenTofu 1.10+, GitHub CLI 2.60+ signed in, AWS CLI 2.32+) and offers
to install whatever is missing. If there are no usable AWS credentials, it
runs `aws login`: sign in to the AWS console as the root user or an
administrator. Every step checks what already exists first, so re-running it
after a failure picks up where it stopped.

The only things it asks you to do are the ones no API can do for you:
create the Cloudflare token, click the SES verification link, merge the
pull request (or say yes and it merges), and connect your MCP client.
"""

import argparse
import email.parser
import email.policy
import getpass
import json
import io
import itertools
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from typing import NoReturn

try:
    import boto3
    import botocore.exceptions
    import botocore.session
except ImportError:
    print("\n==> Checking tools\n    boto3 is not installed for this Python.")
    if input("    Install it now with pip? [Y/n] ").strip().lower() in ("", "y", "yes"):
        subprocess.run([sys.executable, "-m", "pip", "install", "boto3"], check=True)
        os.execv(sys.executable, [sys.executable, *sys.argv])
    sys.exit(f"Install it with: {sys.executable} -m pip install boto3")

if "DcrConfiguration" not in (
    botocore.session.get_session()
    .get_service_model("cognito-idp")
    .operation_model("UpdateUserPool")
    .input_shape.members
):
    print(
        "\n==> Checking tools\n    This boto3 is too old to turn on Cognito "
        "dynamic client registration."
    )
    if input("    Upgrade it now with pip? [Y/n] ").strip().lower() in ("", "y", "yes"):
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "boto3"], check=True
        )
        os.execv(sys.executable, [sys.executable, *sys.argv])
    sys.exit(f"Upgrade it with: {sys.executable} -m pip install --upgrade boto3")

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "infra" / "scripts"))

from allow_email_codes import USER_AUTH, allow_email_codes  # noqa: E402
from enable_dcr import enable_dcr  # noqa: E402

RULE_SET = "pidge"
FUNCTION = "pidge"
POOL = "pidge"
PR = "1"
LOGIN_PROFILE = "pidge-login"
SETUP_PROFILE = "pidge-setup"
MIN_AWS_CLI = (2, 32, 0)
MIN_GH = (2, 60, 0)
MIN_TOFU = (1, 10, 0)
AWS_CLI_INSTALL = (
    "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
)
CLOUDFLARE = "https://api.cloudflare.com/client/v4"
POLL = 10


def say(text: str) -> None:
    print(f"\n==> {text}", flush=True)


def ok(text: str) -> None:
    print(f"    ok: {text}", flush=True)


RERUN = "Then run the same setup command again; steps already done are skipped."
REMEDIES = {
    "gh": (
        "If the error above is about permissions or authentication, run\n"
        "      gh auth refresh -h github.com -s repo,workflow\n"
        "    (the script needs admin access to the repository's secrets,\n"
        "    variables and workflows)."
    ),
    "tofu": (
        "If the error above mentions AWS credentials or an expired token, just\n"
        "    rerun: the script signs you in again. Otherwise the error is in the\n"
        "    AWS or Cloudflare resources it names; paste it to Claude."
    ),
    "aws": "Run that aws command yourself to see its full error.",
    "brew": "Run `brew doctor` and follow what it says.",
}


def fail(cause: str, remedy: str) -> NoReturn:
    print(f"\n!!  {cause}\n    What to do: {remedy}\n    {RERUN}", file=sys.stderr)
    sys.exit(1)


def run(
    *command: str,
    stdin: str | None = None,
    quiet: bool = True,
    remedy: str | None = None,
) -> str:
    result = subprocess.run(
        command,
        input=stdin,
        text=True,
        stdout=subprocess.PIPE if quiet else None,
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        fail(
            f"`{' '.join(command)}` failed (exit {result.returncode}); "
            "its error is printed above.",
            remedy or REMEDIES.get(command[0], "Read the error printed above."),
        )
    return result.stdout or ""


def wait_for(check, what: str, timeout: float, remedy: str, every: float = POLL):
    """Poll ``check`` until it returns something truthy."""
    deadline = time.time() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.time() > deadline:
            fail(f"Gave up waiting for {what} after {int(timeout)} seconds.", remedy)
        time.sleep(every)


# -- AWS credentials -----------------------------------------------------------


def aws_works(profile: str | None, region: str) -> str | None:
    """Return the account ID if this profile's credentials work."""
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        return session.client("sts").get_caller_identity()["Account"]
    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError):
        return None


def aws_cli_version() -> tuple[int, ...] | None:
    if not shutil.which("aws"):
        return None
    result = subprocess.run(["aws", "--version"], capture_output=True, text=True)
    words = (result.stdout or result.stderr).split()
    version = words[0].partition("/")[2] if words else ""
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


# -- Tools ---------------------------------------------------------------------

BIN = pathlib.Path.home() / ".local" / "bin"
AWS_CLI_DIR = pathlib.Path.home() / ".local" / "aws-cli"


def confirm(question: str) -> bool:
    return input(f"    {question} [Y/n] ").strip().lower() in ("", "y", "yes")


def arch() -> str:
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else "amd64"


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as response:
        return response.read()


def latest_release(repo: str) -> str:
    """The latest release's version, from GitHub's /releases/latest redirect."""
    with urllib.request.urlopen(
        f"https://github.com/{repo}/releases/latest", timeout=30
    ) as response:
        return response.geturl().rstrip("/").rsplit("/", 1)[-1].lstrip("v")


def place(data: bytes, name: str) -> None:
    BIN.mkdir(parents=True, exist_ok=True)
    target = BIN / name
    target.write_bytes(data)
    target.chmod(0o755)


def install_tofu() -> None:
    if shutil.which("brew"):
        run("brew", "install", "opentofu", quiet=False)
        return
    version = latest_release("opentofu/opentofu")
    system = "darwin" if sys.platform == "darwin" else "linux"
    url = (
        "https://github.com/opentofu/opentofu/releases/download/"
        f"v{version}/tofu_{version}_{system}_{arch()}.zip"
    )
    print(f"    downloading {url}", flush=True)
    with zipfile.ZipFile(io.BytesIO(download(url))) as archive:
        place(archive.read("tofu"), "tofu")


def install_gh() -> None:
    if shutil.which("brew"):
        run("brew", "install", "gh", quiet=False)
        return
    if sys.platform == "darwin":
        fail(
            "There is no Homebrew to install the GitHub CLI with.",
            "Install Homebrew (https://brew.sh), or download gh from "
            "https://github.com/cli/cli/releases and put it on your PATH.",
        )
    version = latest_release("cli/cli")
    url = (
        "https://github.com/cli/cli/releases/download/"
        f"v{version}/gh_{version}_linux_{arch()}.tar.gz"
    )
    print(f"    downloading {url}", flush=True)
    with tarfile.open(fileobj=io.BytesIO(download(url))) as archive:
        member = next(m for m in archive.getmembers() if m.name.endswith("/bin/gh"))
        extracted = archive.extractfile(member)
        assert extracted is not None
        place(extracted.read(), "gh")


def install_aws() -> None:
    if shutil.which("brew"):
        installed = aws_cli_version() is not None
        run("brew", "upgrade" if installed else "install", "awscli", quiet=False)
        return
    if sys.platform == "darwin":
        fail(
            "There is no Homebrew to install the AWS CLI with.",
            f"Install Homebrew (https://brew.sh), or the AWS CLI pkg from {AWS_CLI_INSTALL}",
        )
    machine = "aarch64" if arch() == "arm64" else "x86_64"
    url = f"https://awscli.amazonaws.com/awscli-exe-linux-{machine}.zip"
    print(f"    downloading {url}", flush=True)
    with tempfile.TemporaryDirectory() as scratch:
        zipfile.ZipFile(io.BytesIO(download(url))).extractall(scratch)
        installer = pathlib.Path(scratch) / "aws" / "install"
        for path in installer.parent.rglob("*"):
            if path.is_file():
                path.chmod(0o755)
        command = [str(installer), "-i", str(AWS_CLI_DIR), "-b", str(BIN)]
        if AWS_CLI_DIR.exists():
            command.append("--update")
        run(*command, quiet=False)


def aws_cli_ok() -> bool:
    return (aws_cli_version() or ()) >= MIN_AWS_CLI


def version_of(command: list[str], position: int) -> tuple[int, ...]:
    """Run a --version command and parse the dotted version at word ``position``."""
    if not shutil.which(command[0]):
        return ()
    result = subprocess.run(command, capture_output=True, text=True)
    words = (result.stdout or result.stderr).split()
    version = words[position].lstrip("v") if len(words) > position else ""
    parts = []
    for part in version.split("."):
        digits = "".join(itertools.takewhile(str.isdigit, part))
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def gh_ok() -> bool:
    """gh 2.60 or later; checked against every gh command this script runs."""
    return version_of(["gh", "--version"], 2) >= MIN_GH


def tofu_ok() -> bool:
    """OpenTofu 1.10 or later: the state backend uses S3 native locking."""
    return version_of(["tofu", "version"], 1) >= MIN_TOFU


TOOLS = (
    (
        "OpenTofu 1.10 or later (tofu)",
        tofu_ok,
        install_tofu,
        "brew install opentofu, or https://opentofu.org/docs/intro/install/",
    ),
    (
        "GitHub CLI 2.60 or later (gh)",
        gh_ok,
        install_gh,
        "brew install gh, or https://github.com/cli/cli#installation",
    ),
    (
        "AWS CLI 2.32 or later (aws)",
        aws_cli_ok,
        install_aws,
        f"brew install awscli, or {AWS_CLI_INSTALL}",
    ),
)


def check_tools() -> None:
    """Check every tool before doing anything, offering to install what's missing."""
    say("Checking tools")
    original_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{BIN}{os.pathsep}{original_path}"
    for name, present, install, remedy in TOOLS:
        if present():
            ok(name)
            continue
        print(f"    missing: {name}. To install it yourself: {remedy}", flush=True)
        where = "with Homebrew" if shutil.which("brew") else f"into {BIN}"
        if not confirm(f"Install it now {where}?"):
            fail(f"{name} is needed and you chose not to install it.", remedy)
        try:
            install()
        except (
            OSError,
            KeyError,
            StopIteration,
            zipfile.BadZipFile,
            tarfile.TarError,
        ) as error:
            fail(f"Installing {name} failed: {error}", remedy)
        if not present():
            fail(
                f"{name} was installed, but the check still fails "
                f"(`which` finds {shutil.which(name.split('(')[-1].rstrip(')'))}).",
                f"Remove the older copy from your PATH, or: {remedy}",
            )
        ok(f"{name} installed")
    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode:
        print("    The GitHub CLI is not signed in.", flush=True)
        if not confirm("Sign in now (gh auth login)?"):
            fail("The GitHub CLI is not signed in.", "Run `gh auth login`.")
        if subprocess.run(["gh", "auth", "login"]).returncode:
            fail(
                "`gh auth login` did not finish.",
                "Run `gh auth login` and pick GitHub.com, HTTPS, and log in with a "
                "web browser.",
            )
    ok("GitHub CLI signed in")
    installed_here = [t for t in ("tofu", "gh", "aws") if (BIN / t).exists()]
    if installed_here and str(BIN) not in original_path.split(os.pathsep):
        print(
            f"    note: {', '.join(installed_here)} are in {BIN}; add it to your PATH."
        )


def in_container() -> bool:
    """Tell whether this runs in a container (Docker, Podman, Colima)."""
    markers = ("/.dockerenv", "/run/.containerenv")
    return any(os.path.exists(path) for path in markers) or bool(
        os.environ.get("container")
    )


def aws_credentials(region: str) -> None:
    """Make AWS credentials available to boto3 and OpenTofu, signing in if needed.

    Credentials that already work (keys, SSO, environment) are used as they
    are. Otherwise the script runs `aws login` (console sign-in) and points
    AWS_PROFILE at a profile whose credential_process reads that sign-in, so
    OpenTofu, which cannot read `aws login` sessions itself, can use it too.
    The sign-in lasts up to 12 hours; a later run signs in again if needed.
    """
    say("Checking AWS credentials")
    try:
        scoped = botocore.session.get_session().get_scoped_config()
    except botocore.exceptions.ProfileNotFound:
        scoped = {}
    current = os.environ.get("AWS_PROFILE") or "default"
    if not scoped.get("login_session"):
        account = aws_works(None, region)
        if account:
            ok(f"using your existing credentials (account {account})")
            return
    login_profile = current if scoped.get("login_session") else LOGIN_PROFILE
    process = (
        f"aws configure export-credentials --profile {login_profile} --format process"
    )
    run(
        "aws",
        "configure",
        "set",
        "credential_process",
        process,
        "--profile",
        SETUP_PROFILE,
    )
    run("aws", "configure", "set", "region", region, "--profile", SETUP_PROFILE)
    os.environ.pop("AWS_ACCESS_KEY_ID", None)
    os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
    os.environ.pop("AWS_SESSION_TOKEN", None)
    os.environ["AWS_PROFILE"] = SETUP_PROFILE
    account = aws_works(SETUP_PROFILE, region)
    if not account:
        if login_profile == LOGIN_PROFILE:
            run("aws", "configure", "set", "region", region, "--profile", LOGIN_PROFILE)
        say("Signing in to AWS")
        command = ["aws", "login", "--profile", login_profile]
        if in_container():
            command.append("--remote")
            print(
                "    In a container, the browser can't reach the CLI's callback, so:\n"
                "    open the link below on your computer, sign in to the AWS console\n"
                "    as the root user or an administrator, and paste the code it\n"
                "    shows back here.",
                flush=True,
            )
        else:
            print(
                "    A browser window opens. Sign in to the AWS console as the root\n"
                "    user or an administrator, then allow the AWS CLI.",
                flush=True,
            )
        if subprocess.run(command).returncode:
            fail(
                "`aws login` did not finish.",
                "Sign in as the account's root user (it needs nothing extra). An IAM "
                "user needs the managed policy first: aws iam attach-user-policy "
                "--user-name YOU --policy-arn "
                "arn:aws:iam::aws:policy/SignInLocalDevelopmentAccess",
            )
        account = aws_works(SETUP_PROFILE, region)
    if not account:
        fail(
            f"Signed in, but the {SETUP_PROFILE} profile still has no credentials.",
            f"Run `aws configure export-credentials --profile {login_profile}` to "
            "see the error; `aws logout --profile "
            f"{login_profile}` clears a stuck session.",
        )
    ok(f"signed in (account {account}); using AWS_PROFILE={SETUP_PROFILE}")


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

    def show_account(self) -> None:
        say(f"AWS account {self.account}, region {self.region}")

    def check_rule_set(self) -> None:
        say("Checking for another active SES receipt rule set")
        active = self.ses.describe_active_receipt_rule_set().get("Metadata", {})
        name = active.get("Name")
        if name and name != RULE_SET:
            print(
                f"    SES rule set {name!r} is active in {self.region}. Only one can be\n"
                f"    active, and deploying makes {RULE_SET!r} active instead, so mail\n"
                f"    that {name!r} handles today would stop being received."
            )
            if not confirm(f"Deactivate {name!r} now and continue?"):
                fail(
                    f"SES rule set {name!r} is active in {self.region}.",
                    f"Move its rules into {RULE_SET!r} after the deploy, or rerun with "
                    "--region set to another region that supports SES receiving.",
                )
            self.ses.set_active_receipt_rule_set()
            ok(f"deactivated {name!r}")
            return
        ok("none besides Pidge's own" if name else "none active")

    def cloudflare(
        self, path: str, token: str, method: str = "GET", body: dict | None = None
    ) -> dict:
        request = urllib.request.Request(
            f"{CLOUDFLARE}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def token_problem(self, token: str) -> tuple[str | None, str, list[dict]]:
        """Check a Cloudflare token: (problem or None, zone ID, MX records)."""
        try:
            zones = self.cloudflare(
                f"/zones?name={urllib.parse.quote(self.domain)}", token
            )["result"]
        except OSError as error:
            return f"Cloudflare rejected the token ({error}).", "", []
        if not zones:
            return (
                f"The token works but cannot see the zone {self.domain}: its Zone "
                f"Resources must include {self.domain}, and the zone must be in "
                "that Cloudflare account.",
                "",
                [],
            )
        zone = zones[0]["id"]
        check = f"_pidge-setup-check.{self.domain}"
        try:
            for stale in self.cloudflare(
                f"/zones/{zone}/dns_records?type=TXT&name={check}", token
            )["result"]:
                self.cloudflare(
                    f"/zones/{zone}/dns_records/{stale['id']}", token, "DELETE"
                )
            probe = self.cloudflare(
                f"/zones/{zone}/dns_records",
                token,
                "POST",
                dict(type="TXT", name=check, content="pidge setup check", ttl=60),
            )["result"]
            self.cloudflare(f"/zones/{zone}/dns_records/{probe['id']}", token, "DELETE")
        except OSError as error:
            return (
                f"The token can read {self.domain} but not edit its DNS ({error}); "
                "it needs Zone > DNS > Edit.",
                "",
                [],
            )
        records = self.cloudflare(
            f"/zones/{zone}/dns_records?type=MX&name={urllib.parse.quote(self.domain)}",
            token,
        )["result"]
        return None, zone, records

    def cloudflare_token(self) -> None:
        say("Cloudflare API token")
        token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not token and "CLOUDFLARE_API_TOKEN" in self.secret_names():
            ok("already stored as a GitHub secret")
            return
        instructions = (
            "    Create one at https://dash.cloudflare.com/profile/api-tokens\n"
            "    (Create Token > Create Custom Token), named pidge-dns, with:\n"
            "      Permissions: Zone > DNS > Edit   and   Zone > Zone > Read\n"
            f"      Zone Resources: Include > Specific zone > {self.domain}"
        )
        while True:
            if not token:
                print(instructions)
                token = getpass.getpass("    Paste the token (input hidden): ").strip()
            problem, zone, records = self.token_problem(token)
            if problem is None:
                break
            print(f"    {problem}")
            if not confirm("Paste a corrected token?"):
                fail(problem, "Create a token as described above.")
            token = ""
        ok(f"token can edit {self.domain} (added and removed a test TXT record)")
        ours = f"inbound-smtp.{self.region}.amazonaws.com"
        others = [r for r in records if r["content"].rstrip(".") != ours]
        if others:
            listed = ", ".join(r["content"] for r in others)
            print(
                f"    {self.domain} has MX records that would compete with SES: "
                f"{listed}.\n    Mail sent to {self.domain} could go there instead."
            )
            if not confirm("Delete them now?"):
                fail(
                    f"{self.domain} has MX records that would compete with SES: {listed}",
                    f"Delete them in the Cloudflare dashboard (DNS > Records for "
                    f"{self.domain}).",
                )
            for record in others:
                self.cloudflare(
                    f"/zones/{zone}/dns_records/{record['id']}", token, "DELETE"
                )
            ok(f"deleted {listed}")
        ok("no MX record competes with SES")
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
            "BUDGET_LIMIT_USD": self.args.budget_limit,
        }
        for name, value in values.items():
            self.gh("variable", "set", name, "--body", value)
        ok(", ".join(values))

    def deploy(self) -> None:
        say(f"Merging pull request #{PR} and waiting for the deploy")
        pr = json.loads(
            self.gh("pr", "view", PR, "--json", "state,mergeCommit,isDraft")
        )
        if pr["state"] == "OPEN":
            answer = input(f"    Merge pull request #{PR} now? [y/N] ").strip().lower()
            if answer == "y":
                if pr["isDraft"]:
                    self.gh("pr", "ready", PR)
                    ok(f"marked pull request #{PR} ready for review")
                self.gh("pr", "merge", PR, "--merge", quiet=False)
            else:
                print("    Waiting for you to merge it on GitHub...")
            pr = wait_for(
                lambda: (lambda p: p if p["state"] == "MERGED" else None)(
                    json.loads(self.gh("pr", "view", PR, "--json", "state,mergeCommit"))
                ),
                "the pull request to merge",
                timeout=24 * 3600,
                remedy=f"Merge it: gh pr ready {PR} --repo {self.repo} && "
                f"gh pr merge {PR} --merge --repo {self.repo}",
            )
        elif pr["state"] != "MERGED":
            if not confirm(f"Pull request #{PR} is closed. Reopen it?"):
                fail(
                    f"Pull request #{PR} is closed.",
                    f"Reopen it: gh pr reopen {PR} --repo {self.repo}",
                )
            self.gh("pr", "reopen", PR)
            self.deploy()
            return
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
        wait_for(
            lambda: self.latest_deploy(sha),
            "the deploy run to start",
            600,
            remedy="Start it: gh workflow run deploy.yml --ref trunk "
            f"--repo {self.repo}",
        )
        # The newest run on trunk may be a later fix; it supersedes this one.
        newest = self.latest_deploy(None)
        assert newest is not None
        if newest["status"] == "completed":
            if newest["conclusion"] == "success":
                ok("the latest deploy succeeded")
                return
            print("    The last deploy failed; deploying the current trunk again.")
            self.redeploy()
            return
        run_id = str(newest["databaseId"])
        print(f"    Watching https://github.com/{self.repo}/actions/runs/{run_id}")
        result = subprocess.run(
            ["gh", "run", "watch", run_id, "--exit-status", "--repo", self.repo],
            cwd=ROOT,
        )
        if result.returncode != 0:
            self.gh("run", "view", run_id, "--log-failed", quiet=False)
            fail(
                "The deploy failed; the failed step's log is printed above.",
                f"If it is a transient AWS error, rerun this script and it deploys "
                f"again. Otherwise the fix goes into the repository; rerun this "
                f"script once it is on trunk (log: https://github.com/{self.repo}/"
                f"actions/runs/{run_id}).",
            )
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
            remedy=f"Check the Actions tab: https://github.com/{self.repo}/actions",
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

            wait_for(
                verified,
                "the SES verification link",
                timeout=24 * 3600,
                remedy="Look for the email from no-reply-aws@amazon.com (check spam) "
                "and click its link.",
            )
        try:
            identity = self.sesv2.get_email_identity(EmailIdentity=self.domain)
        except self.sesv2.exceptions.NotFoundException:
            fail(
                f"SES in {self.region} has no identity for {self.domain}.",
                "The deploy creates it. Check that the last deploy succeeded "
                f"(gh run list --workflow deploy.yml --repo {self.repo}) and that "
                f"--region matches the deploy's region ({self.region} here).",
            )
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

        key, message_id = wait_for(
            arrived,
            "the test message to arrive",
            300,
            remedy=f"Check that {self.domain}'s MX record points at "
            f"inbound-smtp.{self.region}.amazonaws.com (dig +short MX {self.domain}) "
            f"and that the {RULE_SET!r} rule set is active in SES.",
        )
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

        ulid = wait_for(
            ingested,
            "the message to be ingested",
            300,
            remedy="Check the function's log: aws logs tail /aws/lambda/pidge "
            f"--since 15m --region {self.region}",
            every=5,
        )
        ok(f"ingested as msgs/{ulid}/ (it is in your inbox, tagged inbox)")

    def pool(self) -> str:
        pools = self.cognito.list_user_pools(MaxResults=60)["UserPools"]
        matches = [p["Id"] for p in pools if p["Name"] == POOL]
        if not matches:
            fail(
                f"There is no Cognito user pool named {POOL!r} in {self.region}.",
                "The deploy creates it; check that the last deploy succeeded: "
                f"gh run list --workflow deploy.yml --repo {self.repo}",
            )
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
            remedy="In Claude, add the connector with the URL above and click Connect.",
            every=5,
        )
        allow_email_codes(self.cognito, pool_id)
        print(
            f"    Now click Connect again and sign in as {self.email} with the code\n"
            '    emailed to you ("Pidge sign-in"). You should see six tools.'
        )

    def all(self) -> None:
        self.show_account()
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
        "--email",
        required=True,
        help="your own address, outside the mail domain: the mailbox owner, "
        "and where budget alerts go",
    )
    parser.add_argument("--budget-limit", default="5", help="monthly budget in USD")
    parser.add_argument("--domain", default="ncolony.org")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--repo", default="moshez/pidge")
    parser.add_argument(
        "--cognito-prefix",
        default=None,
        help="managed-login domain prefix (default pidge-<domain label>)",
    )
    args = parser.parse_args()
    if args.email.lower().endswith(f"@{args.domain.lower()}"):
        parser.error("--email must be outside the mail domain")
    if args.cognito_prefix is None:
        args.cognito_prefix = f"pidge-{args.domain.split('.')[0]}"
    check_tools()
    aws_credentials(args.region)
    Setup(args).all()


if __name__ == "__main__":
    main()
