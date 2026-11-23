# spotify-sync - deploy to AWS Lambda with the plain AWS CLI (no SAM/CDK needed).
# Safe to re-run: creates what's missing, updates what exists.
#
#   .\aws\deploy.ps1                                 # us-west-2, hourly
#   .\aws\deploy.ps1 -AlertEmail you@example.com     # also email on repeated failures
#
# Prereq: the three SSM parameters exist (see aws\README.md).
param(
    [string]$Region = "us-west-2",
    [string]$Name = "spotify-sync",
    [string]$Schedule = "rate(1 hour)",
    [string]$ParamPrefix = "/spotify-sync",
    [string]$AlertEmail = ""
)
$ErrorActionPreference = "Stop"
$env:AWS_DEFAULT_REGION = $Region
$env:AWS_PAGER = ""

function Aws { aws @args; if ($LASTEXITCODE -ne 0) { throw "aws $($args -join ' ') failed" } }
function Exists { aws @args *> $null; return ($LASTEXITCODE -eq 0) }

$Root = Split-Path $PSScriptRoot -Parent
$Build = Join-Path $PSScriptRoot "build"
$Zip = Join-Path $PSScriptRoot "function.zip"
$Account = (Aws sts get-caller-identity --query Account --output text).Trim()
Write-Host "Deploying $Name to account $Account in $Region" -ForegroundColor Green

# --- package (standard library + boto3, which Lambda already provides)
Remove-Item $Build, $Zip -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $Build | Out-Null
Copy-Item "$Root\spotify_sync.py", "$Root\config.toml", "$PSScriptRoot\lambda_function.py" $Build
Compress-Archive -Path "$Build\*" -DestinationPath $Zip -Force

function Write-Json($obj) {
    $f = New-TemporaryFile
    # UTF-8 without BOM so the AWS CLI can parse it
    [IO.File]::WriteAllText($f, ($obj | ConvertTo-Json -Depth 10 -Compress))
    return "file://$f"
}
function Ensure-Role($roleName, $service, $inlinePolicy, $managedArn) {
    if (-not (Exists iam get-role --role-name $roleName)) {
        $trust = @{ Version = "2012-10-17"; Statement = @(@{ Effect = "Allow"; Principal = @{ Service = $service }; Action = "sts:AssumeRole" }) }
        Aws iam create-role --role-name $roleName --assume-role-policy-document (Write-Json $trust) | Out-Null
        $script:NewRole = $true
    }
    if ($managedArn) { Aws iam attach-role-policy --role-name $roleName --policy-arn $managedArn }
    Aws iam put-role-policy --role-name $roleName --policy-name inline --policy-document (Write-Json $inlinePolicy)
    return "arn:aws:iam::${Account}:role/$roleName"
}

# --- Lambda execution role: logs + read/write its own SSM parameters only
$NewRole = $false
$ssmPolicy = @{ Version = "2012-10-17"; Statement = @(@{
    Effect = "Allow"; Action = @("ssm:GetParameters", "ssm:PutParameter")
    Resource = "arn:aws:ssm:${Region}:${Account}:parameter$ParamPrefix/*" }) }
$LambdaRole = Ensure-Role "$Name-lambda" "lambda.amazonaws.com" $ssmPolicy "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

$FnArn = "arn:aws:lambda:${Region}:${Account}:function:$Name"
$SchedRole = Ensure-Role "$Name-scheduler" "scheduler.amazonaws.com" @{ Version = "2012-10-17"; Statement = @(@{
    Effect = "Allow"; Action = "lambda:InvokeFunction"; Resource = $FnArn }) } $null
if ($NewRole) { Write-Host "Waiting for new IAM roles to propagate..."; Start-Sleep 12 }

# --- log group with short retention (keeps CloudWatch at $0)
$lg = (Aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/$Name" --query "length(logGroups[?logGroupName=='/aws/lambda/$Name'])" --output text).Trim()
if ($lg -eq "0") { Aws logs create-log-group --log-group-name "/aws/lambda/$Name" }
Aws logs put-retention-policy --log-group-name "/aws/lambda/$Name" --retention-in-days 14

# --- function
if (Exists lambda get-function --function-name $Name) {
    Write-Host "Updating function code..."
    Aws lambda update-function-code --function-name $Name --zip-file "fileb://$Zip" | Out-Null
    Aws lambda wait function-updated --function-name $Name
} else {
    Write-Host "Creating function..."
    Aws lambda create-function --function-name $Name --runtime python3.13 --architectures arm64 `
        --handler lambda_function.handler --role $LambdaRole --timeout 120 --memory-size 128 `
        --environment "Variables={PARAM_PREFIX=$ParamPrefix}" --zip-file "fileb://$Zip" | Out-Null
    Aws lambda wait function-active-v2 --function-name $Name
}

# --- hourly schedule (EventBridge Scheduler)
$target = Write-Json @{ Arn = $FnArn; RoleArn = $SchedRole; Input = "{}"; RetryPolicy = @{ MaximumRetryAttempts = 0 } }
$verb = if (Exists scheduler get-schedule --name $Name) { "update-schedule" } else { "create-schedule" }
Aws scheduler $verb --name $Name --schedule-expression $Schedule --flexible-time-window Mode=OFF --target $target | Out-Null

# --- optional failure email (3 failed hours in a row)
if ($AlertEmail) {
    $topic = (Aws sns create-topic --name "$Name-alerts" --query TopicArn --output text).Trim()
    Aws sns subscribe --topic-arn $topic --protocol email --notification-endpoint $AlertEmail | Out-Null
    Aws cloudwatch put-metric-alarm --alarm-name "$Name-failing" --namespace AWS/Lambda --metric-name Errors `
        --dimensions "Name=FunctionName,Value=$Name" --statistic Sum --period 3600 --evaluation-periods 3 `
        --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data notBreaching `
        --alarm-actions $topic
    Write-Host "Check $AlertEmail and confirm the SNS subscription." -ForegroundColor Yellow
}

Remove-Item $Build -Recurse -Force
Write-Host "Done. Runs on '$Schedule'. Run it now with:  aws lambda invoke --function-name $Name out.json" -ForegroundColor Cyan
