# Moving a hand-built deployment onto the CDK stack

This runbook is for a spotify-sync deployment that was created by hand (with `aws` CLI calls or the
old `aws/deploy.ps1`) and should now be managed by the CDK stack in this folder.

The stack uses the **same physical names** as the hand-built setup, so CloudFormation can't create
its resources while the hand-built ones exist. The switch is therefore: prepare everything, back up,
delete the hand-built resources, `cdk deploy`. The Spotify secrets in SSM Parameter Store are never
touched, so **you don't have to approve Spotify again**.

**Downtime:** the every-minute sync is down from the moment you delete the schedules until
`cdk deploy` finishes, typically 5 to 10 minutes. Nothing is lost: the first run afterwards reads the
last 50 plays and the newest likes, and the daily full check re-verifies everything. Don't do it
between 3:30 and 5:00 AM (your `timezone`), when the daily full check and the Monday playback
jobs run.

All commands are PowerShell, run from the repo root unless a step says otherwise.

## What changes and what doesn't

| | |
|---|---|
| **Preserved** | All SSM parameters under `/spotify-sync`: `client-id`, `client-secret`, `refresh-token`, `refresh-token-issued`, `link-key`, `user-id`, `last-reminded`, `state/*`. Same function, role, topic, alarm, schedule and log group names. |
| **Changes** | The approval function gets a **new function URL**. Add its `/callback` to your Spotify app's Redirect URIs (step 6). Renewal links you were emailed before stop working; new reminders contain the new link. |
| **Changes** | The SNS topic is recreated, so you must **confirm the subscription email again**. |
| **Changes** | Old CloudWatch logs are deleted with the hand-built log groups (they only kept 14 days anyway). |
| **Changes** | Both functions get `TOPIC_ARN` and `PARAM_PREFIX` only. `spotify-sync-auth` no longer has `AUTH_BASE_URL` (see below). The code package keeps the repo layout, so the handlers are `aws.lambda_function.handler` and `aws.auth_function.handler`. |
| **New** | The stack owns one plain String parameter, `/spotify-sync/auth-url`, holding the new function URL. |
| **Adopted** | The playlist cache bucket `spotify-sync-cache-<account>-<region>` and its HTTPS-only policy are imported into the stack as they are (step 3b), so the cached playlists survive and the first runs after the deploy don't re-read All Weeklies (~75 requests). Both are retained on `cdk destroy`. |

### Why `AUTH_BASE_URL` isn't needed any more

`base_url()` in `aws/auth_function.py` looks for the public URL in this order:

1. the `AUTH_BASE_URL` environment variable (hand-built only);
2. the value cached by an earlier call in the same warm container;
3. for HTTP requests through the function URL (`/start`, `/callback`): the request's own
   `requestContext.domainName`, i.e. the URL Spotify and your browser actually used;
4. otherwise (the scheduled `{"action":"remind"}` and `{"action":"link"}` invocations): the SSM
   parameter `<prefix>/auth-url`, which the stack writes from the function URL.

The stack's role allows `ssm:GetParameters` on `/spotify-sync/*`, so step 4 works. Steps 3 and 4 give
the same URL (`https://<id>.lambda-url.<region>.on.aws/`), so the `redirect_uri` sent in `/start` matches
the one sent in `/callback`, as Spotify requires.

## 1. Prerequisites (once per computer)

- [Node.js](https://nodejs.org/) LTS and the CDK CLI: `npm install -g aws-cdk`, then `cdk --version`.
- Python 3.11+.
- [AWS CLI v2](https://aws.amazon.com/cli/), signed in to the account the live deployment is in.
  A recent v2 is needed only if you roll back (`--invoked-via-function-url`).

```powershell
$env:AWS_REGION = "us-west-2"; $env:AWS_DEFAULT_REGION = "us-west-2"; $env:CDK_DEFAULT_REGION = "us-west-2"
$env:AWS_PAGER = ""
aws sts get-caller-identity            # Account must be the one the live functions are in
aws lambda get-function --function-name spotify-sync --query Configuration.FunctionArn --output text
$Account = aws sts get-caller-identity --query Account --output text
```

Keep this PowerShell window open: later steps use `$Account`, `$B` and the region variables.

## 2. Prepare the CDK app (nothing changes in AWS yet, apart from bootstrap)

```powershell
cd cdk
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cdk bootstrap                          # once per account + region; harmless if already done
cdk ls --long                          # SpotifySync must show your account and us-west-2
cdk synth -c alertEmail=you@example.com > $null
cdk diff  -c alertEmail=you@example.com
cd ..
```

`cdk synth` warns `spotify-sync:noAlertEmail` if you forget `-c alertEmail=...`. Use the same address
the hand-built topic has (step 3 saves it).

Check what you are about to deploy:

- `cdk diff` lists only additions (`[+]`): 2 functions, 2 log groups, 2 IAM roles with their
  policies, the function URL and its 2 permissions, the topic and subscription, 5 schedules,
  the alarm, `/spotify-sync/auth-url`, and the cache bucket with its bucket policy (HTTPS only).
- The code package contains exactly 4 files:

  ```powershell
  Get-ChildItem cdk\cdk.out -Directory -Filter "asset.*" | ForEach-Object { Get-ChildItem $_.FullName -Recurse -File | ForEach-Object FullName }
  ```

  Expected: `aws\auth_function.py`, `aws\lambda_function.py`, `config.toml`, `spotify_sync.py`.
- `config.toml` in your working copy is the one you want live (the deploy uploads it).

## 2b. Smoke test the stack (optional, recommended; the live deployment isn't touched)

`-c smoke=true` deploys a throwaway copy, stack `SpotifySyncSmoke`. Every name ends in `-smoke`, it
reads its own SSM prefix `/spotify-sync-smoke`, its schedules are created **switched off**, and it
sends no email. It never gets the real Spotify secrets: the sync writes back a rotated refresh token,
so a second copy holding the real one could invalidate the live sync's token. With dummy credentials
it can still prove the parts most likely to break in a real deploy: the code package and handler
paths, the IAM permissions, the public function URL and its two permissions, `auth-url`, and the
schedules. It takes about 5 minutes and costs nothing noticeable.

```powershell
cd cdk
cdk deploy -c smoke=true --require-approval never
cd ..
$Smoke = "/spotify-sync-smoke"
aws ssm put-parameter --name "$Smoke/client-id"     --value smoke-test --type String --overwrite > $null
aws ssm put-parameter --name "$Smoke/client-secret" --value smoke-test --type String --overwrite > $null
Set-Content smoke-dry.json '{"dry_run":true}'
```

**1. Sync function: package, handler, SSM read.**

```powershell
aws lambda invoke --function-name spotify-sync-smoke --cli-binary-format raw-in-base64-out --payload file://smoke-dry.json smoke-sync.json > $null
Get-Content smoke-sync.json
```

Expected: `{"ok": true, "skipped": "not approved yet"}`. `Runtime.ImportModuleError` or
`Runtime.HandlerNotFound` means a packaging or handler-path problem; `AccessDeniedException` means the role.

**2. Approval function: `auth-url` and SSM write.**

```powershell
aws lambda invoke --function-name spotify-sync-smoke-auth --cli-binary-format raw-in-base64-out --payload file://cdk/link.json smoke-link.json > $null
$Link = (Get-Content smoke-link.json | ConvertFrom-Json).approval_link; $Link
```

Expected: `https://<id>.lambda-url.us-west-2.on.aws/start?k=<64 hex characters>`.

**3. Public URL and its permissions.** Don't open the link in a browser (there's nothing to approve).

```powershell
curl.exe -s -o NUL -w "%{http_code} %{redirect_url}`n" $Link
curl.exe -s -w "`n%{http_code}`n" ($Link -replace 'k=.*', 'k=wrong')
```

Expected: first `302 https://accounts.spotify.com/authorize?...client_id=smoke-test...`; then the
function's own "Link not valid" page and `403`. If either returns `{"Message":"Forbidden"}`, the
function URL's permissions are missing (see the aws-cdk-lib version note in `requirements.txt`).

**4. Schedules.**

```powershell
aws scheduler get-schedule --name spotify-sync-smoke --query "{State:State,Tz:ScheduleExpressionTimezone,Target:Target.Arn,Input:Target.Input}"
aws scheduler list-schedules --name-prefix spotify-sync-smoke --query "Schedules[].[Name,State]" --output text
```

Expected: 5 schedules, all `DISABLED`, timezone `America/Los_Angeles`, target `...:function:spotify-sync-smoke`.

**5. Remove the copy.**

```powershell
cd cdk; cdk destroy -c smoke=true --force; cd ..
aws s3 rb "s3://spotify-sync-smoke-cache-$Account-us-west-2"      # retained by design; it's empty
foreach ($p in "client-id", "client-secret", "link-key") { aws ssm delete-parameter --name "$Smoke/$p" }
aws ssm get-parameters-by-path --path $Smoke --recursive --query "Parameters[].Name" --output text   # expect nothing
Remove-Item smoke-*.json
```

Not covered (it needs real Spotify credentials): actual Spotify calls and the S3 cache permissions.
Step 7 checks those on the real stack, and the cache permissions are the same statements the
hand-built role already uses successfully.

## 3. Back up the hand-built setup (read-only)

The backup goes outside the repo, so it can't end up in a commit or the code package. It contains no
secret values.

```powershell
$B = "$HOME\spotify-sync-backup"; New-Item -ItemType Directory -Force $B | Out-Null
function Save($name) { $input | Set-Content -Encoding ascii (Join-Path $B $name) }
$Functions = "spotify-sync", "spotify-sync-auth"
$Roles     = "spotify-sync-lambda", "spotify-sync-scheduler"
$Schedules = "spotify-sync", "spotify-sync-full", "spotify-sync-reminder", "spotify-sync-playback", "spotify-sync-playback-final"
$Topic     = "arn:aws:sns:${env:AWS_REGION}:${Account}:spotify-sync-alerts"

foreach ($f in $Functions) {
    aws lambda get-function-configuration --function-name $f | Save "$f-config.json"
    $loc = aws lambda get-function --function-name $f --query Code.Location --output text
    Invoke-WebRequest $loc -OutFile (Join-Path $B "$f.zip")
}
aws lambda get-function-url-config --function-name spotify-sync-auth | Save "spotify-sync-auth-url.json"
aws lambda get-policy --function-name spotify-sync-auth --query Policy --output text | Save "spotify-sync-auth-policy.json"
foreach ($r in $Roles) {
    aws iam get-role --role-name $r | Save "role-$r.json"
    aws iam list-attached-role-policies --role-name $r | Save "role-$r-attached.json"
    foreach ($p in ((aws iam list-role-policies --role-name $r --query PolicyNames --output text) -split '\s+' | Where-Object { $_ })) {
        aws iam get-role-policy --role-name $r --policy-name $p | Save "role-$r-inline-$p.json"
    }
}
foreach ($s in $Schedules) { aws scheduler get-schedule --name $s | Save "schedule-$s.json" }
aws sns list-subscriptions-by-topic --topic-arn $Topic | Save "topic-subscriptions.json"
aws cloudwatch describe-alarms --alarm-names spotify-sync-failing | Save "alarm.json"
aws ssm get-parameters-by-path --path /spotify-sync --recursive --query "Parameters[].Name" --output text | Save "ssm-names.txt"
Get-ChildItem $B
```

Every command must succeed and every file must be non-empty. `topic-subscriptions.json` shows the
email address to pass as `alertEmail`. `ssm-names.txt` lists the parameter names (no values).

The stack creates `/spotify-sync/auth-url`, so it must not exist yet:

```powershell
aws ssm get-parameter --name /spotify-sync/auth-url --query Parameter.Value --output text
```

`ParameterNotFound` is what you want. If it exists, it's only a URL (not a secret): delete it in step 4.

## 3b. Adopt the cache bucket into the stack (the live sync keeps running)

The hand-made cache bucket has the same name as the stack's, so instead of deleting and recreating it
(which empties the cache), `cdk import` makes it part of the stack as it is. `-c importCache=true`
synthesizes a template with only the bucket and its policy, at the same logical IDs as the full stack;
`--no-version-reporting` leaves out CDK's metadata resource, which can't be imported. This creates the
`SpotifySync` stack holding just those two resources and changes nothing about the bucket, its
policy or its contents, so the hand-built functions keep using it.

```powershell
$Cache = "spotify-sync-cache-$Account-$env:AWS_REGION"
aws s3api get-bucket-policy --bucket $Cache --query Policy --output text | Save "cache-bucket-policy.json"
'{"PlaylistCache31D9056C":{"BucketName":"' + $Cache + '"},"PlaylistCachePolicy6E59AAD1":{"Bucket":"' + $Cache + '"}}' |
  Set-Content -Encoding ascii (Join-Path $B "cache-import-map.json")
cd cdk
cdk import -c importCache=true --no-version-reporting --resource-mapping (Join-Path $B "cache-import-map.json") --force
cd ..
aws cloudformation describe-stack-resources --stack-name SpotifySync --query "StackResources[].[LogicalResourceId,ResourceStatus]" --output text
aws s3api list-objects-v2 --bucket $Cache --query "KeyCount"      # same number of cached playlists as before
```

Expected: the two `PlaylistCache...` resources, `UPDATE_COMPLETE`. If the logical IDs in the error
don't match, the stack code changed the bucket's construct path: check with
`cdk synth -c importCache=true --no-version-reporting` and update the map.

To undo before step 4: `cd cdk; cdk destroy --force; cd ..` removes the stack and **keeps** the bucket
and its policy (both are retained), so the hand-built setup is exactly as before.

## 4. Delete the hand-built resources (sync is down from here)

Run these in order. Each one should print nothing or a small JSON result, not an error.

```powershell
# 4a. Stop all invocations
foreach ($s in $Schedules) { aws scheduler delete-schedule --name $s }
aws cloudwatch delete-alarms --alarm-names spotify-sync-failing

# 4b. Let a run that is already in progress finish (up to 5 minutes). Repeat until it says
#     ParameterNotFound; otherwise the new function skips runs as "locked" for up to 6 minutes.
aws ssm get-parameter --name /spotify-sync/lock --query Parameter.Value --output text

# 4c. Functions (this also deletes the function URL and its resource policy), then their log groups
foreach ($f in $Functions) { aws lambda delete-function --function-name $f }
Start-Sleep 5
foreach ($f in $Functions) { aws logs delete-log-group --log-group-name "/aws/lambda/$f" }

# 4d. Roles: detach managed policies, delete inline policies, delete the role
foreach ($r in $Roles) {
    foreach ($a in ((aws iam list-attached-role-policies --role-name $r --query "AttachedPolicies[].PolicyArn" --output text) -split '\s+' | Where-Object { $_ })) {
        aws iam detach-role-policy --role-name $r --policy-arn $a
    }
    foreach ($p in ((aws iam list-role-policies --role-name $r --query PolicyNames --output text) -split '\s+' | Where-Object { $_ })) {
        aws iam delete-role-policy --role-name $r --policy-name $p
    }
    aws iam delete-role --role-name $r
}

# 4e. Topic (this also deletes its email subscription)
aws sns delete-topic --topic-arn $Topic

# 4f. Only if step 3 found it
# aws ssm delete-parameter --name /spotify-sync/auth-url

# Do NOT delete the cache bucket: since step 3b it belongs to the stack.
```

Don't migrate while Spotify's quota is exhausted (`/spotify-sync/state/backoff-until` in the future):
the cache survives, but the first runs still check every job against Spotify.

If `list-attached-role-policies` showed a customer-managed policy (`arn:aws:iam::<account>:policy/...`),
delete it too after detaching (`aws iam delete-policy --policy-arn ...`); the stack doesn't need it.

Do **not** delete anything else under `/spotify-sync` in SSM.

## 5. Deploy the stack

```powershell
cd cdk
cdk deploy -c alertEmail=you@example.com
cd ..
```

Approve the IAM changes when asked. When it finishes, it prints `SpotifySync.AuthUrl`,
`SpotifySync.SpotifyRedirectUri` and `SpotifySync.GetApprovalLink`. The every-minute schedule is
live again as soon as the deploy completes.

If the deploy fails, CloudFormation rolls back what it created. The usual cause is a name that still
exists (`... already exists`): delete that resource as in step 4, then run `cdk deploy` again (the CLI
removes a stack left in `ROLLBACK_COMPLETE` before retrying; `cdk destroy` also works). If you'd rather
go back to the hand-built setup, see [Roll back](#roll-back).

## 6. After the deploy

1. **Confirm the SNS subscription.** AWS emails *AWS Notification - Subscription Confirmation* to
   `alertEmail`. Click the link (it expires after 3 days). Until then, no reminders or alerts arrive.
2. **Add the new redirect URI in Spotify.** On <https://developer.spotify.com/dashboard>, open your app,
   *Edit* → *Redirect URIs*, paste `SpotifySync.SpotifyRedirectUri`, click **Add**, then **Save**.
   The old `/callback` URI (in `$B\spotify-sync-auth-url.json`) can be removed once everything works.
3. **Get the new renewal link** (the old one no longer works):

   ```powershell
   aws lambda invoke --function-name spotify-sync-auth --cli-binary-format raw-in-base64-out --payload file://cdk/link.json link-out.json
   Get-Content link-out.json
   ```

   Keep `approval_link`. Opening it is optional: Spotify skips the consent screen for an app you already
   approved and you should land on *All set ✅*. That proves the redirect URI and the function URL
   permissions work, and it restarts the 6-month token clock.

## 7. Verify

```powershell
# a dry run of the sync, with its log
'{"dry_run": true}' | Set-Content -Encoding ascii dry.json
aws lambda invoke --function-name spotify-sync --cli-binary-format raw-in-base64-out --payload file://dry.json --log-type Tail out.json --query LogResult --output text |
  % { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_)) }
Get-Content out.json                    # {"ok": true, ...}

# scheduled runs (wait 2-3 minutes after the deploy)
aws logs tail /aws/lambda/spotify-sync --since 10m
aws logs tail /aws/lambda/spotify-sync-auth --since 1h

# schedules, timezone, targets
aws scheduler list-schedules --name-prefix spotify-sync --query "Schedules[].[Name,State]" --output table
foreach ($s in $Schedules) { aws scheduler get-schedule --name $s --query "[Name,ScheduleExpression,ScheduleExpressionTimezone,Target.Input,Target.RetryPolicy.MaximumRetryAttempts]" --output text }

# the rest
aws lambda get-policy --function-name spotify-sync-auth --query Policy --output text   # 2 statements: InvokeFunctionUrl + InvokeFunction (InvokedViaFunctionUrl)
aws sns list-subscriptions-by-topic --topic-arn $Topic --query "Subscriptions[].[Endpoint,SubscriptionArn]" --output text   # not PendingConfirmation
aws cloudwatch describe-alarms --alarm-names spotify-sync-failing --query "MetricAlarms[].[AlarmName,StateValue]" --output text
aws ssm get-parameters-by-path --path /spotify-sync --recursive --query "Parameters[].Name" --output text   # same names as ssm-names.txt, plus /spotify-sync/auth-url
cd cdk; cdk diff -c alertEmail=you@example.com; cd ..    # "There were no differences"
```

You should see 5 schedules, `ENABLED`, in `America/Los_Angeles`. The log should show runs every minute
without `Waiting for first Spotify approval` (that would mean `refresh-token` is missing).

From now on, change things with `cdk deploy` only. Delete `$B` once you no longer need a rollback.

## Roll back

`cdk destroy` removes everything the stack created (including `/spotify-sync/auth-url`) and leaves the
other SSM parameters alone. The cache bucket and its policy are retained, with their contents, so the
rebuilt functions can use them as before. You can then rebuild the hand-built setup from the backup in `$B`.

The old function URL can't be restored: a recreated function gets a new URL, so add its `/callback` in the
Spotify dashboard again, as in step 6.

```powershell
cd cdk; cdk destroy; cd ..            # skip if the deploy failed and nothing is left

$B = "$HOME\spotify-sync-backup"
function J($name) { Get-Content -Raw (Join-Path $B $name) | ConvertFrom-Json }
function TmpJson($obj) { $f = New-TemporaryFile; [IO.File]::WriteAllText($f, ($obj | ConvertTo-Json -Depth 20 -Compress)); "file://$f" }

# roles
foreach ($r in $Roles) {
    aws iam create-role --role-name $r --assume-role-policy-document (TmpJson (J "role-$r.json").Role.AssumeRolePolicyDocument) | Out-Null
    foreach ($a in (J "role-$r-attached.json").AttachedPolicies) { aws iam attach-role-policy --role-name $r --policy-arn $a.PolicyArn }
    foreach ($file in Get-ChildItem $B -Filter "role-$r-inline-*.json") {
        $p = Get-Content -Raw $file.FullName | ConvertFrom-Json
        aws iam put-role-policy --role-name $r --policy-name $p.PolicyName --policy-document (TmpJson $p.PolicyDocument)
    }
}
Start-Sleep 15                        # new roles take a few seconds to be usable

# topic (confirm the email again)
$Topic = aws sns create-topic --name spotify-sync-alerts --query TopicArn --output text
aws sns subscribe --topic-arn $Topic --protocol email --notification-endpoint you@example.com

# log groups and functions, from the saved code and configuration
foreach ($f in $Functions) {
    aws logs create-log-group --log-group-name "/aws/lambda/$f"
    aws logs put-retention-policy --log-group-name "/aws/lambda/$f" --retention-in-days 14
    $c = J "$f-config.json"
    aws lambda create-function --function-name $f --runtime $c.Runtime --architectures $c.Architectures[0] `
        --handler $c.Handler --role $c.Role --timeout $c.Timeout --memory-size $c.MemorySize `
        --environment (TmpJson @{ Variables = $c.Environment.Variables }) --zip-file "fileb://$B\$f.zip" | Out-Null
    aws lambda wait function-active-v2 --function-name $f
}

# public URL for the approval function (a NEW URL), its two permissions, and AUTH_BASE_URL
$Url = aws lambda create-function-url-config --function-name spotify-sync-auth --auth-type NONE --query FunctionUrl --output text
aws lambda add-permission --function-name spotify-sync-auth --statement-id PublicFunctionUrl `
    --action lambda:InvokeFunctionUrl --principal "*" --function-url-auth-type NONE | Out-Null
aws lambda add-permission --function-name spotify-sync-auth --statement-id PublicInvokeViaUrl `
    --action lambda:InvokeFunction --principal "*" --invoked-via-function-url | Out-Null
$vars = @{}; (J "spotify-sync-auth-config.json").Environment.Variables.PSObject.Properties | % { $vars[$_.Name] = $_.Value }
$vars.AUTH_BASE_URL = $Url
aws lambda update-function-configuration --function-name spotify-sync-auth --environment (TmpJson @{ Variables = $vars }) | Out-Null
"New redirect URI for Spotify: ${Url}callback"

# schedules, exactly as saved
foreach ($s in $Schedules) {
    $o = J "schedule-$s.json"
    foreach ($k in "Arn", "CreationDate", "LastModificationDate") { $o.PSObject.Properties.Remove($k) }
    aws scheduler create-schedule --cli-input-json (TmpJson $o) | Out-Null
}

# failure alarm
aws cloudwatch put-metric-alarm --alarm-name spotify-sync-failing --namespace AWS/Lambda --metric-name Errors `
    --dimensions Name=FunctionName,Value=spotify-sync --statistic Sum --period 3600 --evaluation-periods 3 `
    --threshold 10 --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data notBreaching `
    --alarm-actions $Topic
```

Then verify as in step 7 (skip the `cdk diff`), and get the new approval link with step 6.3.
