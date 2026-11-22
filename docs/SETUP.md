# Setting up Pidge

One script does the whole setup:

```sh
pip install boto3
python3 infra/setup.py --email you@example.net
```

Run it from the repository root on a machine signed in to AWS (administrator
credentials for the account), with the GitHub CLI signed in (`gh auth
status`) and OpenTofu 1.10 or later installed. Your address is passed on the
command line and stored as a GitHub variable, so it never lands in this
public repository.

Every step checks what already exists before doing anything, so if the
script stops (a failed deploy, a closed laptop), fix what it reports and run
the same command again.

## What it does

1. Checks that no other SES receipt rule set is active in us-west-2 (only
   one can be, and the stack makes its own active).
2. Asks for a Cloudflare API token, verifies it can edit the zone, checks
   that no existing MX record would compete with SES, and stores the token
   as the `CLOUDFLARE_API_TOKEN` secret.
3. Applies `infra/bootstrap`: the Terraform state bucket, GitHub's OIDC
   provider, the deploy role (trusting only `trunk` of this repository, by
   immutable IDs it looks up), and the ECR repository.
4. Sets the repository variables `AWS_DEPLOY_ROLE_ARN`, `TF_STATE_BUCKET`,
   `MAIL_DOMAIN`, `OWNER_EMAIL`, `COGNITO_DOMAIN_PREFIX`.
5. Offers to merge the pull request (or waits while you merge it), then
   follows the deploy run to the end.
6. Waits for you to click the SES verification link sent to your address.
7. Requests SES production access, unless it is already granted or pending.
8. Sends a message to the new mailbox and follows it through SES receiving,
   S3 and ingestion. It reads the received `Message-ID` and, if SES uses a
   different domain than expected, sets `MESSAGE_ID_DOMAIN` and redeploys.
9. Makes sure Cognito dynamic client registration is on.
10. Prints the MCP URL, waits for your MCP client to register, and lets that
    client offer sign-in by emailed code.

## The parts that need you

The script stops and tells you when one of these comes up:

- **The Cloudflare token.** Cloudflare has no API to create the first token.
  At <https://dash.cloudflare.com/profile/api-tokens>: Create Token, Create
  Custom Token, name `pidge-dns`, permissions Zone / DNS / Edit and Zone /
  Zone / Read, zone resources Include / Specific zone / `ncolony.org`. Paste
  it when asked (input is hidden).
- **Merging.** Answer `y` and the script merges, or merge on GitHub yourself.
- **The SES verification email** ("Amazon Web Services – Email Address
  Verification Request in region US West (Oregon)"). Click the link. Until
  the account leaves the SES sandbox, mail (including sign-in codes) only
  reaches verified addresses.
- **Connecting your MCP client.** In Claude: Settings, Connectors, Add custom
  connector, paste the URL the script prints, Add, Connect. Close the
  sign-in page that opens; the script notices the new client and fixes it
  up. Click Connect again and sign in with the emailed code. The client
  should list six tools.

If you add another MCP client later, run the script again after it
registers; only the last step has anything left to do.

## Options

| Option | Default |
|---|---|
| `--email` | (required) your own address, the mailbox owner |
| `--domain` | `ncolony.org` (its DNS must be on Cloudflare) |
| `--region` | `us-west-2` |
| `--repo` | `moshez/pidge` |
| `--cognito-prefix` | `pidge-<first label of the domain>`; must be unique in the region |

## If something fails

- **The deploy cannot assume the role** ("Not authorized to perform
  sts:AssumeRoleWithWebIdentity"): the role trusts
  `repo:<owner>@<owner id>/<repo>@<repo id>:ref:refs/heads/trunk`. Compare
  with `gh api repos/moshez/pidge/actions/oidc/customization/sub`.
- **"Domain already exists"** in the deploy: the Cognito prefix is taken.
  Run the script again with another `--cognito-prefix`.
- **The first deploy times out waiting for SES domain verification** (capped
  at 60 minutes): run the script again; it re-runs the deploy.

## Where problems show up

Nobody gets paged. The reconciler runs every 15 minutes and either fixes a
problem or writes a message tagged `ops` into the inbox:

- inbound mail that failed to ingest five times (stored with the
  `ingest-failed` tag and the raw message intact),
- the search indexer failing twice in a row,
- outgoing mail stuck after three attempts,
- events that Lambda gave up on and that failed three replays (kept under
  `failed-events/` in the bucket).

Search with `tags: ["ops"]` to see them. Each problem is reported at most
once a day.
