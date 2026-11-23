# Running spotify-sync on AWS

AWS Lambda runs the sync every hour, and EventBridge Scheduler triggers it.
Secrets are kept in SSM Parameter Store. At this size, everything stays inside AWS's always-free limits:

| Piece | Usage per month | Free allowance | Cost |
|---|---|---|---|
| Lambda (128 MB, arm64, ~3 s per run) | ~720 runs, ~270 GB-s | 1M requests, 400k GB-s | $0 |
| EventBridge Scheduler | ~720 invocations | 14M invocations | $0 |
| SSM Parameter Store (standard SecureString) | 3 parameters | standard tier is free | $0 |
| CloudWatch Logs (14-day retention) | a few KB | 5 GB | $0 |
| Optional failure alarm + email | 1 alarm | 10 alarms, 1k emails | $0 |

`deploy.ps1` uses only the AWS CLI. It runs in Windows PowerShell, and in PowerShell 7 (`pwsh`) on macOS or Linux.

## 1. Get Spotify credentials

Follow steps 1–2 of the main README: create the Spotify app, then run `python spotify_sync.py auth` to get a refresh token.

## 2. Store them in SSM (once)

```powershell
$env:AWS_DEFAULT_REGION = "us-west-2"
aws ssm put-parameter --name /spotify-sync/client-id     --type SecureString --value "YOUR_CLIENT_ID"
aws ssm put-parameter --name /spotify-sync/client-secret --type SecureString --value "YOUR_CLIENT_SECRET"
aws ssm put-parameter --name /spotify-sync/refresh-token --type SecureString --value "YOUR_REFRESH_TOKEN"
```

Add `--overwrite` to replace an existing value.
If Spotify ever issues a new refresh token, the function saves it back to `/spotify-sync/refresh-token` automatically.

> Git Bash on Windows rewrites arguments that start with `/` into paths.
> Run these commands from PowerShell, or prefix them with `MSYS_NO_PATHCONV=1`.

## 3. Edit `config.toml`, then deploy

```powershell
.\aws\deploy.ps1                                   # hourly, us-west-2
.\aws\deploy.ps1 -AlertEmail you@example.com       # plus an email if 3 runs in a row fail
.\aws\deploy.ps1 -Schedule "rate(6 hours)" -Region us-east-1
```

`config.toml` is packaged with the function, so re-run `deploy.ps1` after you edit it.
The script is safe to re-run. It creates two IAM roles (`spotify-sync-lambda`, `spotify-sync-scheduler`) with minimal permissions,
the function, the schedule and a log group.

## 4. Test

```powershell
'{"dry_run": true}' | Set-Content -Encoding ascii dry.json
aws lambda invoke --function-name spotify-sync --cli-binary-format raw-in-base64-out --payload file://dry.json --log-type Tail out.json --query LogResult --output text |
  % { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_)) }
```

This prints the function's log, for example `[Liked Songs backup] destination has 812 tracks; Would add 3 new.`
Drop `--payload` to do a real run. To read past runs: `aws logs tail /aws/lambda/spotify-sync --since 1d`.

## Remove everything

```powershell
aws scheduler delete-schedule --name spotify-sync
aws lambda delete-function --function-name spotify-sync
aws logs delete-log-group --log-group-name /aws/lambda/spotify-sync
foreach ($r in "spotify-sync-lambda","spotify-sync-scheduler") {
  aws iam delete-role-policy --role-name $r --policy-name inline
}
aws iam detach-role-policy --role-name spotify-sync-lambda --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name spotify-sync-lambda; aws iam delete-role --role-name spotify-sync-scheduler
aws ssm delete-parameters --names /spotify-sync/client-id /spotify-sync/client-secret /spotify-sync/refresh-token
```
