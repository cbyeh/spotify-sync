# Running spotify-sync on AWS (CDK)

The whole AWS setup is one CDK stack (`cdk/`). It contains:

- **`spotify-sync`**: a Lambda that syncs **every minute**, triggered by EventBridge Scheduler. Each run first asks Spotify "anything new?"
  (1–2 small calls, about a second) and only reads or writes playlists when something changed. A daily full check re-verifies everything.
- **`spotify-sync-auth`**: a Lambda with a public URL that handles Spotify approval, plus a daily check that emails you a one-tap renewal link before Spotify's 6-month token expiry.
- An SNS email topic, a failure alarm, two IAM roles with minimal permissions, and logs kept for 14 days.

A small private S3 bucket (created by the stack) caches the contents of your destination playlists by
Spotify snapshot ID, so big playlists aren't re-read on every Lambda cold start. Spotify's Development Mode
has a daily quota, and re-reading a 7,500-song playlist is what exhausts it.

Your secrets live in SSM Parameter Store, **outside** the stack. Deleting or redeploying the stack never touches them.
The only parameter the stack owns is `/spotify-sync/auth-url` (the approval function's public URL, which the function reads
for reminder emails).

**Already running a hand-built deployment** (created with the AWS CLI or the old `deploy.ps1`)? The stack uses the same
resource names, so follow [cdk/MIGRATION.md](../cdk/MIGRATION.md) instead of the setup below. It keeps your Spotify approval.

| Piece | Usage per month | Free allowance | Cost |
|---|---|---|---|
| Lambda (2 functions, 128 MB, arm64) | ~44k runs, ~6k GB-s | 1M requests, 400k GB-s | $0 |
| EventBridge Scheduler | ~44k invocations | 14M invocations | $0 |
| SSM Parameter Store (standard) | ~12 parameters, ~130k API calls | standard tier is free | $0 |
| CloudWatch Logs (14-day retention) | ~15 MB | 5 GB | $0 |
| SNS email + 1 CloudWatch alarm | a few emails | 1k emails, 10 alarms | $0 |
| CDK bootstrap bucket (S3) | a few KB of code packages | – | ~$0 |

## Prerequisites (once per computer)

- [Node.js](https://nodejs.org/) (LTS), Python 3.11+ and [AWS CLI v2](https://aws.amazon.com/cli/), already signed in (`aws sts get-caller-identity` works)
- CDK CLI: `npm install -g aws-cdk`. The Python side (`aws-cdk-lib` 2.220 or later) is installed from `cdk/requirements.txt` below.

## Setup

**1. Create the Spotify app.** Follow step 1 of the main README.

**2. Store the app's credentials in SSM.** These are the only secrets you enter yourself.

```powershell
$env:AWS_DEFAULT_REGION = "us-west-2"
aws ssm put-parameter --name /spotify-sync/client-id     --type SecureString --value "YOUR_CLIENT_ID"
aws ssm put-parameter --name /spotify-sync/client-secret --type SecureString --value "YOUR_CLIENT_SECRET"
```

> Git Bash on Windows rewrites arguments that start with `/` into paths.
> Run these commands from PowerShell, or prefix them with `MSYS_NO_PATHCONV=1`.

**3. Edit `config.toml`, then deploy.**

```powershell
cd cdk
python -m venv .venv; .\.venv\Scripts\Activate.ps1      # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
$env:AWS_REGION = "us-west-2"; $env:CDK_DEFAULT_REGION = "us-west-2"
cdk bootstrap                                            # once per AWS account + region
cdk diff   -c alertEmail=you@example.com                 # optional: see what will be created
cdk deploy -c alertEmail=you@example.com
```

Confirm the subscription email that AWS sends you, or the reminders won't arrive.

**4. Add the redirect URI to your Spotify app.** `cdk deploy` prints it as `SpotifySync.SpotifyRedirectUri`.
On developer.spotify.com, open your app → *Edit* → *Redirect URIs*, paste it, click **Add**, then **Save**.

**5. Get your approval link and open it.** Run this from the repo root:

```powershell
aws lambda invoke --function-name spotify-sync-auth --cli-binary-format raw-in-base64-out --payload file://cdk/link.json link-out.json
Get-Content link-out.json
```

Open the `approval_link` and tap **Agree**. Syncing starts within a minute. Until you approve, it quietly skips.

## Renewal: one tap about every 5 months

Spotify refresh tokens expire 6 months after you approve the app. About 5 months in, you'll get an email titled
**"spotify-sync: tap to renew Spotify access"**. Open the link on any device where you're signed in to Spotify.
Reminders repeat weekly until you renew. You can also get the link anytime with step 5.

## Security and cost

- The approval URL is public, but `/start` requires a random 64-character key.
  Malformed keys are rejected before any AWS lookup, so junk traffic costs about as little as Lambda allows.
- The round trip through Spotify carries an HMAC-signed `state` that expires after 15 minutes.
  After the first approval, only the same Spotify account can replace the token.
- If the link leaks, run `aws ssm delete-parameter --name /spotify-sync/link-key`. A new key is generated the next time the link is requested.
- Lambda bills $0.20 per million requests, so even heavy junk traffic is cheap. Still, set an
  [AWS Budget](https://console.aws.amazon.com/billing/home#/budgets) alert (for example $3) on any account you use for hobby projects.

## Test

```powershell
'{"dry_run": true}' | Set-Content -Encoding ascii dry.json
aws lambda invoke --function-name spotify-sync --cli-binary-format raw-in-base64-out --payload file://dry.json --log-type Tail out.json --query LogResult --output text |
  % { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_)) }
```

To read past runs: `aws logs tail /aws/lambda/spotify-sync --since 1d`.

## Update / remove

- **Update:** edit `config.toml` or pull new code, then run `cdk deploy -c alertEmail=you@example.com` again from `cdk/`
  (always pass the same `alertEmail`; without it the email subscription is removed).
  The code package contains only `spotify_sync.py`, `config.toml` and `aws/*.py`.
- **Remove:** run `cdk destroy`. This removes everything except your SSM secrets and sync state. To delete those too:
  `aws ssm delete-parameters --names /spotify-sync/client-id /spotify-sync/client-secret /spotify-sync/refresh-token /spotify-sync/refresh-token-issued /spotify-sync/link-key /spotify-sync/user-id /spotify-sync/last-reminded`
  and the change markers: `aws ssm get-parameters-by-path --path /spotify-sync/state --query "Parameters[].Name" --output text`, then `aws ssm delete-parameters --names <those names>`.
