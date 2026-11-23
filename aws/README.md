# Running spotify-sync on AWS

AWS Lambda runs the sync every hour, and EventBridge Scheduler triggers it.
Secrets are kept in SSM Parameter Store, and a second small Lambda handles Spotify approval and renewal.
Nothing has to run on your computer, and you don't need Python locally.

| Piece | Usage per month | Free allowance | Cost |
|---|---|---|---|
| Lambda (2 functions, 128 MB, arm64) | ~750 runs, a few hundred GB-s | 1M requests, 400k GB-s | $0 |
| EventBridge Scheduler | ~750 invocations | 14M invocations | $0 |
| Lambda function URL (approval link) | a handful of requests | no extra charge | $0 |
| SSM Parameter Store (standard) | ~8 parameters | standard tier is free | $0 |
| SNS email + CloudWatch alarm | a few emails, 1 alarm | 1k emails, 10 alarms | $0 |
| CloudWatch Logs (14-day retention) | a few KB | 5 GB | $0 |

`deploy.ps1` uses only the AWS CLI (a recent v2). It runs in Windows PowerShell, and in PowerShell 7 (`pwsh`) on macOS or Linux.

## Setup

**1. Create the Spotify app.** Follow step 1 of the main README.
Its redirect URI `http://127.0.0.1:8888/callback` is only for the local `auth` command. You'll add a second one in step 4.

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
.\aws\deploy.ps1 -AlertEmail you@example.com
```

AWS emails you a subscription confirmation. Click it, or the reminders won't arrive.

**4. Add the printed redirect URI to your Spotify app.** On developer.spotify.com, open your app → *Edit settings* → *Redirect URIs*, paste the
`https://….lambda-url.us-west-2.on.aws/callback` address, click **Add**, then **Save**. This is a one-time step, because the URL never changes.

**5. Open the printed approval link and tap Agree.** That's it. The hourly sync starts on the next run.
Until you approve, the sync quietly skips each run instead of failing.

## Renewal: one tap about every 5 months

Spotify refresh tokens expire 6 months after you approve the app. About 5 months in, you'll get an email
titled **"spotify-sync: tap to renew Spotify access"**. Open the link on any device where you're signed in to Spotify, such as your phone.
Spotify skips the approval screen if you've already approved the app, so it's usually a single tap. Reminders repeat weekly until you renew.

To find the link again later, re-run `deploy.ps1`. It prints the link every time.

### How the approval link is protected

- The link includes a random 64-character key stored in SSM (`/spotify-sync/link-key`). Without it, `/start` refuses the request.
- The round trip through Spotify carries a signed `state` that expires after 15 minutes, so a callback can't be forged or replayed.
- After the first approval, only the same Spotify account can replace the token.
  If someone else got hold of your link, they couldn't swap in their own account.
- In Development Mode, only users you've added under your app's *User Management* can approve it at all.

If the link leaks anyway, delete the parameter (`aws ssm delete-parameter --name /spotify-sync/link-key`) and re-run `deploy.ps1` to get a new one.

## Test

```powershell
'{"dry_run": true}' | Set-Content -Encoding ascii dry.json
aws lambda invoke --function-name spotify-sync --cli-binary-format raw-in-base64-out --payload file://dry.json --log-type Tail out.json --query LogResult --output text |
  % { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_)) }
```

This prints the function's log, for example `[Liked Songs backup] destination has 812 tracks; Would add 3 new.`
Drop `--payload` to do a real run. To read past runs: `aws logs tail /aws/lambda/spotify-sync --since 1d`.

## Update

Edit `config.toml` or pull new code, then re-run `.\aws\deploy.ps1`. It updates everything in place.

## Remove everything

```powershell
aws scheduler delete-schedule --name spotify-sync
aws scheduler delete-schedule --name spotify-sync-reminder
aws lambda delete-function --function-name spotify-sync
aws lambda delete-function --function-name spotify-sync-auth
aws logs delete-log-group --log-group-name /aws/lambda/spotify-sync
aws logs delete-log-group --log-group-name /aws/lambda/spotify-sync-auth
aws cloudwatch delete-alarms --alarm-names spotify-sync-failing
$acct = aws sts get-caller-identity --query Account --output text
aws sns delete-topic --topic-arn "arn:aws:sns:us-west-2:${acct}:spotify-sync-alerts"
foreach ($r in "spotify-sync-lambda","spotify-sync-scheduler") { aws iam delete-role-policy --role-name $r --policy-name inline }
aws iam detach-role-policy --role-name spotify-sync-lambda --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name spotify-sync-lambda; aws iam delete-role --role-name spotify-sync-scheduler
aws ssm delete-parameters --names /spotify-sync/client-id /spotify-sync/client-secret /spotify-sync/refresh-token `
  /spotify-sync/refresh-token-issued /spotify-sync/link-key /spotify-sync/user-id /spotify-sync/last-reminded
```
