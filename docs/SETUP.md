# Setting up Pidge

These are the one-time steps that cannot be automated. Steps 1–5 happen
**before** the pull request is merged; merging it starts the first deploy
(step 6), and steps 7–12 happen after that deploy.

Throughout, replace:

| Placeholder | Meaning |
|---|---|
| `you@example.net` | Your own email address (the only user of the mailbox) |
| `ncolony.org` | The mail domain (its DNS must be on Cloudflare) |
| `ACCOUNT_ID` | Your AWS account ID (`aws sts get-caller-identity --query Account --output text`) |

Everything lives in **us-west-2**.

## What you need locally

- AWS CLI v2, signed in with administrator credentials for the account.
- OpenTofu 1.10 or later (`tofu version`).
- The GitHub CLI, signed in (`gh auth status`).
- Python 3 with boto3 (`pip install boto3`) for the Cognito helper script in step 10.

## 1. Check that nothing else uses SES receiving in us-west-2

Only one SES receipt rule set can be active per region, and the main stack
makes its own rule set active. Check what is active now:

```sh
aws ses describe-active-receipt-rule-set --region us-west-2
```

If this prints nothing (or an error saying there is no active rule set),
carry on. If it prints a rule set, that rule set will stop receiving mail
after the first deploy; move its rules into Pidge first or pick another
region.

Also check that the domain has no MX records that would compete:

```sh
dig +short MX ncolony.org
```

## 2. Apply the bootstrap stack

The bootstrap stack creates the Terraform state bucket, GitHub's OIDC
provider, the deploy role, and the ECR repository.

First confirm the repository and owner IDs that the deploy role trusts
(defaults are in `infra/bootstrap/main.tf`):

```sh
gh api repos/moshez/pidge --jq '{repo_id: .id, owner_id: .owner.id}'
# expected: {"owner_id":422703,"repo_id":1467341512}
```

Then apply:

```sh
cd infra/bootstrap
tofu init
tofu apply -var "state_bucket=pidge-tfstate-ACCOUNT_ID"
```

Note the outputs; you need `state_bucket` and `deploy_role_arn` in step 4:

```sh
tofu output
```

The bootstrap state stays in `infra/bootstrap/terraform.tfstate` on your
laptop. It holds no secrets; keep it (or commit it to a private place). It
is listed in `.gitignore`.

## 3. Create the Cloudflare API token

1. Open the Cloudflare dashboard → **My Profile** → **API Tokens** →
   **Create Token** → **Create Custom Token**.
2. Name it `pidge-dns`.
3. Permissions: **Zone** → **DNS** → **Edit**, and add a second row
   **Zone** → **Zone** → **Read** (the stack looks the zone up by name).
4. Zone Resources: **Include** → **Specific zone** → `ncolony.org`.
5. **Continue to summary** → **Create Token**, and copy the token.

Store it as a repository secret (the command prompts for the value, so it
does not land in your shell history):

```sh
gh secret set CLOUDFLARE_API_TOKEN --repo moshez/pidge
```

## 4. Set the GitHub repository variables

```sh
gh variable set AWS_DEPLOY_ROLE_ARN   --repo moshez/pidge --body "arn:aws:iam::ACCOUNT_ID:role/pidge-deploy"
gh variable set TF_STATE_BUCKET       --repo moshez/pidge --body "pidge-tfstate-ACCOUNT_ID"
gh variable set MAIL_DOMAIN           --repo moshez/pidge --body "ncolony.org"
gh variable set OWNER_EMAIL           --repo moshez/pidge --body "you@example.net"
gh variable set COGNITO_DOMAIN_PREFIX --repo moshez/pidge --body "pidge-ncolony"
```

- `OWNER_EMAIL` is kept in a variable so your address never appears in this
  public repository. Repository variables are visible to collaborators and in
  workflow logs, not to the public.
- `COGNITO_DOMAIN_PREFIX` must be globally unique across Cognito in the
  region; if the deploy fails with "Domain already exists", pick another.
- Optional: `ECR_REPOSITORY` (default `pidge`) and `MESSAGE_ID_DOMAIN`
  (see step 9).

Check:

```sh
gh variable list --repo moshez/pidge
gh secret list --repo moshez/pidge
```

## 5. Review and merge the pull request

Merging to `trunk` runs `.github/workflows/deploy.yml`.

## 6. Watch the first deploy

```sh
gh run watch --repo moshez/pidge $(gh run list --repo moshez/pidge --workflow deploy --limit 1 --json databaseId --jq '.[0].databaseId')
```

The first apply waits for SES to verify the domain through the
`_amazonses` TXT record. That usually takes a few minutes; the wait is
capped at 60 minutes. If it times out, re-run the workflow:

```sh
gh workflow run deploy --repo moshez/pidge
```

At the end the log prints the outputs. Keep `mcp_url` and `user_pool_id`:

```
mcp_url        = "https://<id>.lambda-url.us-west-2.on.aws/mcp"
user_pool_id   = "us-west-2_XXXXXXXXX"
```

(You can also get them later with `aws lambda get-function-url-config
--function-name pidge --region us-west-2` and `aws cognito-idp
list-user-pools --max-results 10 --region us-west-2`.)

If the run fails at **Configure AWS credentials** with "Not authorized to
perform sts:AssumeRoleWithWebIdentity", the token's subject does not match
the role. Check what GitHub sends:

```sh
gh api repos/moshez/pidge/actions/oidc/customization/sub
cd infra/bootstrap && tofu output github_subject
```

The trusted subject is
`repo:moshez@422703/pidge@1467341512:ref:refs/heads/trunk`.

## 7. Verify your own address with SES (sandbox)

The deploy registers `you@example.net` as an SES identity, and SES emails
it a link titled "Amazon Web Services – Email Address Verification Request
in region US West (Oregon)". **Click the link.** Until the account leaves
the sandbox (step 8), SES only delivers to verified addresses, and that
includes the sign-in codes Cognito sends you.

Check:

```sh
aws ses get-identity-verification-attributes --region us-west-2 \
  --identities you@example.net ncolony.org
```

Both should say `"VerificationStatus": "Success"`. Then check DKIM:

```sh
aws sesv2 get-email-identity --region us-west-2 --email-identity ncolony.org \
  --query '{verified: VerifiedForSendingStatus, dkim: DkimAttributes.Status, mailFrom: MailFromAttributes.MailFromDomainStatus}'
```

Expect `verified: true`, `dkim: SUCCESS`, and (after a while) `mailFrom:
SUCCESS`.

If the link never arrived, resend it:

```sh
aws ses verify-email-identity --region us-west-2 --email-address you@example.net
```

## 8. Request SES production access

Receiving works in the sandbox, and so does sending to verified addresses.
To send to anyone else, request production access (a support case with a
24-hour review target):

```sh
aws sesv2 put-account-details --region us-west-2 \
  --production-access-enabled \
  --mail-type TRANSACTIONAL \
  --website-url "https://ncolony.org" \
  --contact-language EN \
  --additional-contact-email-addresses you@example.net \
  --use-case-description "Personal mailbox for one person on ncolony.org. Outgoing mail is individual replies and new messages written by its owner (a few a day). No marketing or bulk mail and no mailing lists. Bounces and complaints are recorded through a configuration set that publishes to EventBridge, and bounced messages are tagged and surfaced to the owner."
```

Check the status:

```sh
aws sesv2 get-account --region us-west-2 \
  --query '{production: ProductionAccessEnabled, review: Details.ReviewDetails.Status}'
```

## 9. Confirm the Message-ID domain

SES replaces the Message-ID of every message it sends with
`<SES message id>@<region>.amazonses.com`. Pidge patches that same value
into its stored copy of each sent message so that replies thread. Confirm
the domain for this account and region with one test message to yourself:

```sh
aws sesv2 send-email --region us-west-2 \
  --from-email-address "mail@ncolony.org" \
  --destination ToAddresses=you@example.net \
  --content 'Simple={Subject={Data=Message-ID check},Body={Text={Data=Test}}}'
```

The command prints a `MessageId`. Open the message in your mail client,
show the original/raw source, and find the `Message-ID:` header. It should
read `<MessageId@us-west-2.amazonses.com>`.

If the part after `@` differs, set it and re-deploy:

```sh
gh variable set MESSAGE_ID_DOMAIN --repo moshez/pidge --body "<the domain you saw>"
gh workflow run deploy --repo moshez/pidge
```

## 10. Connect your MCP client and sign in for the first time

The deploy turned on Cognito dynamic client registration with
`infra/scripts/enable_dcr.py` (the AWS provider cannot set it yet). Check:

```sh
aws cognito-idp describe-user-pool --region us-west-2 --user-pool-id us-west-2_XXXXXXXXX \
  --query 'UserPool.DcrConfiguration'
```

Expect `EnableStatus: Enabled`, `OpenDcrEnabled: true`,
`OpenDcrGrantTypes: ["code"]`, and the `.../mcp/full` scope under
`AllowedScopes`. If it is missing, run the script by hand:

```sh
AWS_REGION=us-west-2 USER_POOL_ID=us-west-2_XXXXXXXXX \
DCR_SCOPES="$(aws cognito-idp list-resource-servers --region us-west-2 --user-pool-id us-west-2_XXXXXXXXX --max-results 10 --query 'ResourceServers[0].Identifier' --output text)/full" \
  python3 infra/scripts/enable_dcr.py
```

Then:

1. In your MCP client, add a remote MCP server with the `mcp_url` from step
   6. In Claude: **Settings** → **Connectors** → **Add custom connector**,
   paste the URL, and **Add**.
2. Click **Connect**. The client registers itself with Cognito and opens
   the sign-in page. **Close that page for now.**
3. Let the newly registered client offer email codes. Cognito registers
   clients without the `ALLOW_USER_AUTH` flow, and managed login only offers
   passwordless choices to clients that have it:

   ```sh
   AWS_REGION=us-west-2 USER_POOL_ID=us-west-2_XXXXXXXXX python3 infra/scripts/allow_email_codes.py
   ```

   It prints the client it changed. Re-run it whenever you connect a new MCP
   client.
4. Click **Connect** again. On the sign-in page, enter `you@example.net`,
   choose the emailed code if asked, and enter the code from the email
   ("Pidge sign-in").
5. The client should list six tools: `search`, `get_message`,
   `get_attachment`, `list_tags`, `send_email`, `tag_email`.

If the sign-in page only offers a password: choose **Forgot your password?**
and set one through the emailed code. Your account is the only one in the
pool, and the password policy requires 32 characters.

## 11. End-to-end check

1. From another account, send a message to `anything@ncolony.org`.
2. Within a minute, ask your MCP client to search the inbox
   (`search` with `tags: ["inbox"]`). The message appears, unranked until the
   indexer runs.
3. Ask it to reply. While in the sandbox the reply only reaches verified
   addresses; anything else is tagged `rejected` with SES's reason.

## 12. Where problems show up

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
