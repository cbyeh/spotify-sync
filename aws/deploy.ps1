# spotify-sync - deploy to AWS with the plain AWS CLI (no SAM/CDK needed).
# Safe to re-run: creates what's missing, updates what exists.
#
#   .\aws\deploy.ps1 -AlertEmail you@example.com     # recommended: renewal reminders + failure alerts
#   .\aws\deploy.ps1                                 # no email (you'd have to remember renewals yourself)
#
# Prereq: SSM parameters <prefix>/client-id and <prefix>/client-secret (see aws/README.md).
param(
    [string]$Region = "us-west-2",
    [string]$Name = "spotify-sync",
    [string]$Schedule = "rate(1 hour)",
    [string]$ReminderSchedule = "cron(0 17 * * ? *)",   # daily check, 17:00 UTC
    [string]$ParamPrefix = "/spotify-sync",
    [string]$AlertEmail = ""
)
$ErrorActionPreference = "Stop"
$env:AWS_DEFAULT_REGION = $Region
$env:AWS_PAGER = ""

function Aws { aws @args; if ($LASTEXITCODE -ne 0) { throw "aws $($args -join ' ') failed" } }
function TryAws {   # returns output, or $null if the command fails (never throws)
    $ErrorActionPreference = "Continue"
    $out = aws @args 2> $null
    if ($LASTEXITCODE -eq 0) { return $out } else { return $null }
}
function Write-Json($obj) {
    $f = New-TemporaryFile
    [IO.File]::WriteAllText($f, ($obj | ConvertTo-Json -Depth 10 -Compress))   # UTF-8, no BOM
    return "file://$f"
}

$Root = Split-Path $PSScriptRoot -Parent
$Account = (Aws sts get-caller-identity --query Account --output text).Trim()
$SyncName = $Name
$AuthName = "$Name-auth"
$SyncArn = "arn:aws:lambda:${Region}:${Account}:function:$SyncName"
$AuthArn = "arn:aws:lambda:${Region}:${Account}:function:$AuthName"
Write-Host "Deploying $Name to account $Account in $Region" -ForegroundColor Green

# --- secrets check + link key ------------------------------------------------
foreach ($p in "client-id", "client-secret") {
    if (-not (TryAws ssm get-parameter --name "$ParamPrefix/$p" --query Parameter.Name --output text)) {
        throw "Missing SSM parameter $ParamPrefix/$p. See aws/README.md step 2."
    }
}
if (-not (TryAws ssm get-parameter --name "$ParamPrefix/link-key" --query Parameter.Name --output text)) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $key = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    Aws ssm put-parameter --name "$ParamPrefix/link-key" --type SecureString --value $key | Out-Null
    Write-Host "Created approval link key."
}

# --- package -----------------------------------------------------------------
$Build = Join-Path $PSScriptRoot "build"
$Zip = Join-Path $PSScriptRoot "function.zip"
Remove-Item $Build, $Zip -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $Build | Out-Null
Copy-Item "$Root\spotify_sync.py", "$Root\config.toml", "$PSScriptRoot\lambda_function.py", "$PSScriptRoot\auth_function.py" $Build
Compress-Archive -Path "$Build\*" -DestinationPath $Zip -Force

# --- notifications -----------------------------------------------------------
$Topic = (Aws sns create-topic --name "$Name-alerts" --query TopicArn --output text).Trim()
if ($AlertEmail) {
    $subs = TryAws sns list-subscriptions-by-topic --topic-arn $Topic --query "Subscriptions[?Endpoint=='$AlertEmail'].Endpoint" --output text
    if (-not $subs) {
        Aws sns subscribe --topic-arn $Topic --protocol email --notification-endpoint $AlertEmail | Out-Null
        Write-Host "Check $AlertEmail and click the AWS confirmation link." -ForegroundColor Yellow
    }
}

# --- IAM ---------------------------------------------------------------------
$script:NewRole = $false
function Ensure-Role($roleName, $service, $inlinePolicy, $managedArn) {
    if (-not (TryAws iam get-role --role-name $roleName --query Role.Arn --output text)) {
        $trust = @{ Version = "2012-10-17"; Statement = @(@{ Effect = "Allow"; Principal = @{ Service = $service }; Action = "sts:AssumeRole" }) }
        Aws iam create-role --role-name $roleName --assume-role-policy-document (Write-Json $trust) | Out-Null
        $script:NewRole = $true
    }
    if ($managedArn) { Aws iam attach-role-policy --role-name $roleName --policy-arn $managedArn }
    Aws iam put-role-policy --role-name $roleName --policy-name inline --policy-document (Write-Json $inlinePolicy)
    return "arn:aws:iam::${Account}:role/$roleName"
}
$LambdaRole = Ensure-Role "$Name-lambda" "lambda.amazonaws.com" @{ Version = "2012-10-17"; Statement = @(
    @{ Effect = "Allow"; Action = @("ssm:GetParameters", "ssm:GetParameter", "ssm:PutParameter")
       Resource = "arn:aws:ssm:${Region}:${Account}:parameter$ParamPrefix/*" },
    @{ Effect = "Allow"; Action = "sns:Publish"; Resource = $Topic }) } "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
$SchedRole = Ensure-Role "$Name-scheduler" "scheduler.amazonaws.com" @{ Version = "2012-10-17"; Statement = @(@{
    Effect = "Allow"; Action = "lambda:InvokeFunction"; Resource = @($SyncArn, $AuthArn) }) } $null
if ($script:NewRole) { Write-Host "Waiting for new IAM roles to propagate..."; Start-Sleep 12 }

# --- functions ---------------------------------------------------------------
function Deploy-Function($fn, $handler, $envVars) {
    $lg = "/aws/lambda/$fn"
    if ((Aws logs describe-log-groups --log-group-name-prefix $lg --query "length(logGroups[?logGroupName=='$lg'])" --output text).Trim() -eq "0") {
        Aws logs create-log-group --log-group-name $lg
    }
    Aws logs put-retention-policy --log-group-name $lg --retention-in-days 14
    $envFile = Write-Json @{ Variables = $envVars }
    if (TryAws lambda get-function --function-name $fn --query Configuration.FunctionName --output text) {
        Write-Host "Updating $fn..."
        Aws lambda update-function-code --function-name $fn --zip-file "fileb://$Zip" | Out-Null
        Aws lambda wait function-updated-v2 --function-name $fn
        Aws lambda update-function-configuration --function-name $fn --handler $handler --environment $envFile | Out-Null
        Aws lambda wait function-updated-v2 --function-name $fn
    } else {
        Write-Host "Creating $fn..."
        Aws lambda create-function --function-name $fn --runtime python3.13 --architectures arm64 `
            --handler $handler --role $LambdaRole --timeout 120 --memory-size 128 `
            --environment $envFile --zip-file "fileb://$Zip" | Out-Null
        Aws lambda wait function-active-v2 --function-name $fn
    }
}

Deploy-Function $SyncName "lambda_function.handler" @{ PARAM_PREFIX = $ParamPrefix }

# Approval function + public URL (guarded by the secret link key)
$AuthEnv = @{ PARAM_PREFIX = $ParamPrefix; TOPIC_ARN = $Topic }
$AuthUrl = TryAws lambda get-function-url-config --function-name $AuthName --query FunctionUrl --output text
if ($AuthUrl) { $AuthEnv.AUTH_BASE_URL = $AuthUrl.Trim() }
Deploy-Function $AuthName "auth_function.handler" $AuthEnv
if (-not $AuthUrl) {
    $AuthUrl = Aws lambda create-function-url-config --function-name $AuthName --auth-type NONE --query FunctionUrl --output text
    $AuthEnv.AUTH_BASE_URL = $AuthUrl.Trim()
    Aws lambda update-function-configuration --function-name $AuthName --environment (Write-Json @{ Variables = $AuthEnv }) | Out-Null
    Aws lambda wait function-updated-v2 --function-name $AuthName
}
$AuthUrl = $AuthUrl.Trim()
$policy = TryAws lambda get-policy --function-name $AuthName --query Policy --output text
if (-not ($policy -match "PublicFunctionUrl")) {
    Aws lambda add-permission --function-name $AuthName --statement-id PublicFunctionUrl `
        --action lambda:InvokeFunctionUrl --principal "*" --function-url-auth-type NONE | Out-Null
}
if (-not ($policy -match "PublicInvokeViaUrl")) {
    # Required for function URLs created since Oct 2025. Needs a recent AWS CLI v2.
    Aws lambda add-permission --function-name $AuthName --statement-id PublicInvokeViaUrl `
        --action lambda:InvokeFunction --principal "*" --invoked-via-function-url | Out-Null
}

# --- schedules ---------------------------------------------------------------
function Ensure-Schedule($schedName, $expr, $arn, $inputJson) {
    $target = Write-Json @{ Arn = $arn; RoleArn = $SchedRole; Input = $inputJson; RetryPolicy = @{ MaximumRetryAttempts = 0 } }
    $verb = if (TryAws scheduler get-schedule --name $schedName --query Name --output text) { "update-schedule" } else { "create-schedule" }
    Aws scheduler $verb --name $schedName --schedule-expression $expr --flexible-time-window Mode=OFF --target $target | Out-Null
}
Ensure-Schedule $Name $Schedule $SyncArn "{}"
Ensure-Schedule "$Name-reminder" $ReminderSchedule $AuthArn '{"action":"remind"}'

# --- failure alarm -----------------------------------------------------------
if ($AlertEmail) {
    Aws cloudwatch put-metric-alarm --alarm-name "$Name-failing" --namespace AWS/Lambda --metric-name Errors `
        --dimensions "Name=FunctionName,Value=$SyncName" --statistic Sum --period 3600 --evaluation-periods 3 `
        --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data notBreaching `
        --alarm-actions $Topic
}

Remove-Item $Build -Recurse -Force
$LinkKey = (Aws ssm get-parameter --name "$ParamPrefix/link-key" --with-decryption --query Parameter.Value --output text).Trim()
Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "1) In your Spotify app settings, add this Redirect URI (once):" -ForegroundColor Cyan
Write-Host "     ${AuthUrl}callback"
Write-Host "2) Then open this approval link and tap Agree (once now; later only when emailed):" -ForegroundColor Cyan
Write-Host "     ${AuthUrl}start?k=$LinkKey"
Write-Host "   Keep this link private - it's what lets someone renew your token."
if (-not $AlertEmail) { Write-Host "No -AlertEmail given: you won't get renewal reminders." -ForegroundColor Yellow }
