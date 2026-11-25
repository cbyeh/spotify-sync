/**
 * CDK stack for spotify-sync: the whole AWS setup as one CloudFormation stack.
 *
 * Physical names match the original hand-built deployment (functions spotify-sync and
 * spotify-sync-auth, roles spotify-sync-lambda and spotify-sync-scheduler, topic spotify-sync-alerts,
 * alarm spotify-sync-failing, five schedules, two log groups). To move an existing hand-built
 * deployment onto this stack, follow cdk/MIGRATION.md.
 *
 * Every physical name starts with `name` (default "spotify-sync"). A different name gives a fully
 * separate copy, which is how the smoke test (`cdk deploy -c smoke=true`, see MIGRATION.md) runs
 * next to the live deployment without touching it. `schedulesEnabled: false` creates the schedules
 * switched off.
 *
 * Secrets are NOT managed here. You create <prefix>/client-id and <prefix>/client-secret yourself
 * (CloudFormation can't create SecureString parameters, and secrets don't belong in templates).
 * The functions write the rest (refresh-token, link-key, user-id, state/*, ...) at runtime, so they
 * survive `cdk destroy`. The only parameter this stack owns is the plain String <prefix>/auth-url.
 *
 * Needs aws-cdk-lib >= 2.220.0: from that version on, addFunctionUrl({ authType: NONE }) adds both
 * resource policy statements a public function URL needs (lambda:InvokeFunctionUrl, and
 * lambda:InvokeFunction with InvokedViaFunctionUrl=true).
 *
 * The Lambda code itself is Python (spotify_sync.py, aws/*.py); only the infrastructure is TypeScript.
 */
import * as path from "path";
import {
  Annotations,
  Aws,
  CfnOutput,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  aws_cloudwatch as cw,
  aws_cloudwatch_actions as cwActions,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_logs as logs,
  aws_s3 as s3,
  aws_scheduler as scheduler,
  aws_sns as sns,
  aws_sns_subscriptions as subs,
  aws_ssm as ssm,
} from "aws-cdk-lib";
import { Construct } from "constructs";

const REPO_ROOT = path.resolve(__dirname, "..");

// The code asset keeps the repo layout (handlers are aws.lambda_function / aws.auth_function) but
// contains only what the Lambdas need: spotify_sync.py, config.toml, aws/lambda_function.py and
// aws/auth_function.py. Each file is named, not aws/*.py, so other code in aws/ (the spend-cap kill
// switch, which isn't part of this stack) stays out of the package.
// This is an allowlist in CDK's default GLOB ignore mode: "*" and ".*" exclude every entry (with
// matchBase they match any basename, and "*" alone skips dotfiles such as .env), then "!" patterns
// re-include. Excluded directories (cdk/ with its node_modules, cdk.out, .git, .github, tests, docs,
// aws/build, __pycache__) aren't walked at all, so nothing under them can slip back in.
const ASSET_EXCLUDE = [
  "*", ".*",
  "!spotify_sync.py", "!config.toml",
  "!aws", "!aws/lambda_function.py", "!aws/auth_function.py",
];

const LAMBDA_BASIC = "service-role/AWSLambdaBasicExecutionRole";

export interface SpotifySyncStackProps extends StackProps {
  name?: string;
  schedulesEnabled?: boolean;
  cacheOnly?: boolean;
  paramPrefix?: string;
  alertEmail?: string;
  syncSchedule?: string;
  // all schedules below are in `timezone`
  fullCheckSchedule?: string;     // default 3:30 AM daily
  reminderSchedule?: string;      // default 9 AM daily renewal check
  // via = "playback" jobs: Monday 4 AM, one retry at 5 AM that emails if it couldn't run
  playbackSchedule?: string;
  playbackFinalSchedule?: string;
  timezone?: string;
}

export class SpotifySyncStack extends Stack {
  constructor(scope: Construct, id: string, props: SpotifySyncStackProps = {}) {
    super(scope, id, props);
    const {
      name = "spotify-sync",
      schedulesEnabled = true,
      cacheOnly = false,
      paramPrefix = "/spotify-sync",
      alertEmail,
      syncSchedule = "rate(1 minute)",
      fullCheckSchedule = "cron(30 3 * * ? *)",
      reminderSchedule = "cron(0 9 * * ? *)",
      playbackSchedule = "cron(0 4 ? * MON *)",
      playbackFinalSchedule = "cron(0 5 ? * MON *)",
      timezone = "America/Los_Angeles",
    } = props;
    const prefix = "/" + paramPrefix.replace(/^\/+|\/+$/g, "");

    if (cacheOnly) {
      // `-c importCache=true` (MIGRATION.md step 3b): only the cache bucket and its policy, at the
      // same construct paths (so the same logical IDs) as in the full stack. `cdk import` adopts
      // the existing hand-made bucket into the stack with this template; the normal `cdk deploy`
      // afterwards adds everything else around it, and the cached playlists survive.
      this.cacheBucket(name);
      return;
    }

    const code = lambda.Code.fromAsset(REPO_ROOT, { exclude: ASSET_EXCLUDE });

    // --- notifications: renewal reminders + failure alerts
    const topic = new sns.Topic(this, "Alerts", { topicName: `${name}-alerts` });
    if (alertEmail) {
      topic.addSubscription(new subs.EmailSubscription(alertEmail));
    } else {
      Annotations.of(this).addWarningV2(
        "spotify-sync:noAlertEmail",
        "No alertEmail: renewal reminders and failure alerts go nowhere. " +
        "Deploy with -c alertEmail=you@example.com");
    }

    // --- one role for both functions: basic logging + the SSM prefix + publishing to the topic
    const lambdaRole = new iam.Role(this, "LambdaRole", {
      roleName: `${name}-lambda`,
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName(LAMBDA_BASIC)],
    });
    // arn:aws:ssm:<region>:<account>:parameter/spotify-sync/*
    const paramsArn = this.formatArn({
      service: "ssm", resource: "parameter", resourceName: prefix.replace(/^\/+/, "") + "/*",
    });
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      actions: ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath",
                "ssm:PutParameter", "ssm:DeleteParameter"],
      resources: [paramsArn],
    }));
    // renewal reminders (auth) and "couldn't copy Discover Weekly this week" emails (sync)
    topic.grantPublish(lambdaRole);

    // --- playlist cache: each destination's snapshot_id + track URIs, so cold starts and the daily
    //     full check don't re-read big playlists (that's what exhausts Spotify's daily quota).
    //     Only a cache: safe to lose, so it's retained on `cdk destroy` simply to avoid surprises.
    const cacheBucket = this.cacheBucket(name);
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      resources: [cacheBucket.arnForObjects("cache/*")],
    }));
    // Without ListBucket, S3 answers a missing key with AccessDenied instead of NoSuchKey.
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      actions: ["s3:ListBucket"], resources: [cacheBucket.bucketArn],
      conditions: { StringLike: { "s3:prefix": ["cache/*"] } },
    }));

    const makeFunction = (cid: string, fnName: string, handler: string, timeout: Duration) => {
      const logGroup = new logs.LogGroup(this, `${cid}Logs`, {
        logGroupName: `/aws/lambda/${fnName}`,
        retention: logs.RetentionDays.TWO_WEEKS, removalPolicy: RemovalPolicy.DESTROY,
      });
      return new lambda.Function(this, cid, {
        functionName: fnName,
        runtime: lambda.Runtime.PYTHON_3_13, architecture: lambda.Architecture.ARM_64,
        handler, code, memorySize: 128, timeout, role: lambdaRole,
        environment: { PARAM_PREFIX: prefix, TOPIC_ARN: topic.topicArn,
                       CACHE_BUCKET: cacheBucket.bucketName },
        logGroup,
      });
    };

    // 5 minutes: a cold start may have to page through a large archive (~7k tracks ≈ 50 s)
    const syncFn = makeFunction("SyncFunction", name, "aws.lambda_function.handler",
                                Duration.minutes(5));
    const authFn = makeFunction("AuthFunction", `${name}-auth`, "aws.auth_function.handler",
                                Duration.minutes(2));

    // --- public approval URL (guarded in code by the secret link key). With auth type NONE,
    //     aws-cdk-lib >= 2.220.0 adds both statements: lambda:InvokeFunctionUrl
    //     (FunctionUrlAuthType NONE) and lambda:InvokeFunction (InvokedViaFunctionUrl true).
    const url = authFn.addFunctionUrl({ authType: lambda.FunctionUrlAuthType.NONE });
    // The function can't reference its own URL (circular), so it reads it from SSM when the
    // request itself doesn't carry it (scheduled reminders, {"action":"link"}).
    new ssm.StringParameter(this, "AuthUrlParam", {
      parameterName: `${prefix}/auth-url`,
      stringValue: url.url,
      description: `Public URL of ${name}-auth (managed by the ${id} stack)`,
    });

    // --- schedules (EventBridge Scheduler)
    const schedulerRole = new iam.Role(this, "SchedulerRole", {
      roleName: `${name}-scheduler`,
      assumedBy: new iam.ServicePrincipal("scheduler.amazonaws.com"),
    });
    syncFn.grantInvoke(schedulerRole);
    authFn.grantInvoke(schedulerRole);
    const schedules: [string, string, string, lambda.Function, string][] = [
      ["SyncSchedule", name, syncSchedule, syncFn, "{}"],
      // once a day, ignore the "anything new?" shortcut and re-check every playlist
      ["FullCheckSchedule", `${name}-full`, fullCheckSchedule, syncFn, '{"full":true}'],
      ["ReminderSchedule", `${name}-reminder`, reminderSchedule, authFn, '{"action":"remind"}'],
      ["PlaybackSchedule", `${name}-playback`, playbackSchedule, syncFn, '{"playback":true}'],
      ["PlaybackFinalSchedule", `${name}-playback-final`, playbackFinalSchedule, syncFn,
       '{"playback":true,"final":true}'],
    ];
    for (const [cid, schedName, expr, fn, payload] of schedules) {
      const sched = new scheduler.CfnSchedule(this, cid, {
        name: schedName, scheduleExpression: expr, scheduleExpressionTimezone: timezone,
        state: schedulesEnabled ? "ENABLED" : "DISABLED",
        flexibleTimeWindow: { mode: "OFF" },
        target: {
          arn: fn.functionArn, roleArn: schedulerRole.roleArn, input: payload,
          retryPolicy: { maximumRetryAttempts: 0 },
        },
      });
      // Scheduler checks the role when the schedule is created: wait for its policy.
      sched.node.addDependency(schedulerRole);
    }

    // --- email if the sync keeps failing (10+ failed runs an hour, 3 hours in a row);
    //     the odd transient Spotify error at one run per minute won't trigger it
    const alarm = new cw.Alarm(this, "SyncFailing", {
      alarmName: `${name}-failing`,
      metric: syncFn.metricErrors({ period: Duration.hours(1), statistic: "Sum" }),
      threshold: 10, evaluationPeriods: 3,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    });
    alarm.addAlarmAction(new cwActions.SnsAction(topic));

    new CfnOutput(this, "AuthUrl", { value: url.url, description: `Public URL of ${name}-auth` });
    new CfnOutput(this, "SpotifyRedirectUri", {
      value: `${url.url}callback`,
      description: "Add this to your Spotify app's Redirect URIs (once)",
    });
    new CfnOutput(this, "GetApprovalLink", {
      value: `aws lambda invoke --function-name ${name}-auth ` +
             "--cli-binary-format raw-in-base64-out --payload file://cdk/link.json link-out.json",
      description: "Run from the repo root, then open the approval_link in link-out.json",
    });
  }

  private cacheBucket(name: string): s3.Bucket {
    // The same properties as the hand-made bucket (SSE-S3, BucketOwnerEnforced, all public access
    // blocked, HTTPS-only policy), so adopting it with `cdk import` changes nothing about it.
    // `cdk import` needs a DeletionPolicy on every imported resource, so the bucket policy
    // (created by enforceSSL) is retained along with the bucket.
    const bucket = new s3.Bucket(this, "PlaylistCache", {
      bucketName: `${name}-cache-${Aws.ACCOUNT_ID}-${Aws.REGION}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      encryption: s3.BucketEncryption.S3_MANAGED, enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    bucket.policy!.applyRemovalPolicy(RemovalPolicy.RETAIN);
    return bucket;
  }
}
