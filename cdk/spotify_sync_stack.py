"""CDK stack for spotify-sync: the whole AWS setup as one CloudFormation stack.

Physical names match the original hand-built deployment (functions spotify-sync and
spotify-sync-auth, roles spotify-sync-lambda and spotify-sync-scheduler, topic spotify-sync-alerts,
alarm spotify-sync-failing, five schedules, two log groups). To move an existing hand-built
deployment onto this stack, follow cdk/MIGRATION.md.

Secrets are NOT managed here. You create <prefix>/client-id and <prefix>/client-secret yourself
(CloudFormation can't create SecureString parameters, and secrets don't belong in templates).
The functions write the rest (refresh-token, link-key, user-id, state/*, ...) at runtime, so they
survive `cdk destroy`. The only parameter this stack owns is the plain String <prefix>/auth-url.

Needs aws-cdk-lib >= 2.220.0: from that version on, add_function_url(auth_type=NONE) adds both
resource policy statements a public function URL needs (lambda:InvokeFunctionUrl, and
lambda:InvokeFunction with InvokedViaFunctionUrl=true).
"""
import os

from aws_cdk import (
    Annotations,
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_scheduler as scheduler,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_ssm as ssm,
)
from constructs import Construct

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# The code asset keeps the repo layout (handlers are aws.lambda_function / aws.auth_function) but
# contains only what the Lambdas need: spotify_sync.py, config.toml and aws/*.py.
# This is an allowlist in CDK's default GLOB ignore mode: "*" and ".*" exclude every entry (with
# matchBase they match any basename, and "*" alone skips dotfiles such as .env), then "!" patterns
# re-include. Excluded directories (cdk/, cdk.out, .git, .github, .venv, tests, docs, aws/build,
# __pycache__) aren't walked at all, so nothing under them can slip back in.
ASSET_EXCLUDE = [
    "*", ".*",
    "!spotify_sync.py", "!config.toml",
    "!aws", "!aws/*.py",
]

LAMBDA_BASIC = "service-role/AWSLambdaBasicExecutionRole"


class SpotifySyncStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *,
                 param_prefix: str = "/spotify-sync",
                 alert_email: str | None = None,
                 sync_schedule: str = "rate(1 minute)",
                 # all schedules below are in `timezone`
                 full_check_schedule: str = "cron(30 3 * * ? *)",     # 3:30 AM daily
                 reminder_schedule: str = "cron(0 9 * * ? *)",        # 9 AM daily renewal check
                 # via = "playback" jobs: Monday 4 AM, one retry at 5 AM that emails if it couldn't run
                 playback_schedule: str = "cron(0 4 ? * MON *)",
                 playback_final_schedule: str = "cron(0 5 ? * MON *)",
                 timezone: str = "America/Los_Angeles",
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        prefix = "/" + param_prefix.strip("/")

        code = lambda_.Code.from_asset(REPO_ROOT, exclude=ASSET_EXCLUDE)

        # --- notifications: renewal reminders + failure alerts
        topic = sns.Topic(self, "Alerts", topic_name="spotify-sync-alerts")
        if alert_email:
            topic.add_subscription(subs.EmailSubscription(alert_email))
        else:
            Annotations.of(self).add_warning_v2(
                "spotify-sync:noAlertEmail",
                "No alertEmail: renewal reminders and failure alerts go nowhere. "
                "Deploy with -c alertEmail=you@example.com")

        # --- one role for both functions: basic logging + the SSM prefix + publishing to the topic
        lambda_role = iam.Role(
            self, "LambdaRole", role_name="spotify-sync-lambda",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(LAMBDA_BASIC)])
        # arn:aws:ssm:<region>:<account>:parameter/spotify-sync/*
        params_arn = self.format_arn(service="ssm", resource="parameter",
                                     resource_name=prefix.lstrip("/") + "/*")
        lambda_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath",
                     "ssm:PutParameter", "ssm:DeleteParameter"],
            resources=[params_arn]))
        # renewal reminders (auth) and "couldn't copy Discover Weekly this week" emails (sync)
        topic.grant_publish(lambda_role)

        # --- playlist cache: each destination's snapshot_id + track URIs, so cold starts and the daily
        #     full check don't re-read big playlists (that's what exhausts Spotify's daily quota).
        #     Only a cache: safe to lose, so it's retained on `cdk destroy` simply to avoid surprises.
        cache_bucket = s3.Bucket(
            self, "PlaylistCache", bucket_name=f"spotify-sync-cache-{Aws.ACCOUNT_ID}-{Aws.REGION}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            encryption=s3.BucketEncryption.S3_MANAGED, enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN)
        lambda_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            resources=[cache_bucket.arn_for_objects("cache/*")]))

        def make_function(cid: str, name: str, handler: str, timeout: Duration) -> lambda_.Function:
            log_group = logs.LogGroup(
                self, f"{cid}Logs", log_group_name=f"/aws/lambda/{name}",
                retention=logs.RetentionDays.TWO_WEEKS, removal_policy=RemovalPolicy.DESTROY)
            return lambda_.Function(
                self, cid, function_name=name,
                runtime=lambda_.Runtime.PYTHON_3_13, architecture=lambda_.Architecture.ARM_64,
                handler=handler, code=code, memory_size=128, timeout=timeout, role=lambda_role,
                environment={"PARAM_PREFIX": prefix, "TOPIC_ARN": topic.topic_arn,
                             "CACHE_BUCKET": cache_bucket.bucket_name},
                log_group=log_group)

        # 5 minutes: a cold start may have to page through a large archive (~7k tracks ≈ 50 s)
        sync_fn = make_function("SyncFunction", "spotify-sync", "aws.lambda_function.handler",
                                Duration.minutes(5))
        auth_fn = make_function("AuthFunction", "spotify-sync-auth", "aws.auth_function.handler",
                                Duration.minutes(2))

        # --- public approval URL (guarded in code by the secret link key). With auth type NONE,
        #     aws-cdk-lib >= 2.220.0 adds both statements: lambda:InvokeFunctionUrl
        #     (FunctionUrlAuthType NONE) and lambda:InvokeFunction (InvokedViaFunctionUrl true).
        url = auth_fn.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)
        # The function can't reference its own URL (circular), so it reads it from SSM when the
        # request itself doesn't carry it (scheduled reminders, {"action":"link"}).
        ssm.StringParameter(self, "AuthUrlParam", parameter_name=f"{prefix}/auth-url",
                            string_value=url.url,
                            description="Public URL of spotify-sync-auth (managed by the SpotifySync stack)")

        # --- schedules (EventBridge Scheduler)
        scheduler_role = iam.Role(self, "SchedulerRole", role_name="spotify-sync-scheduler",
                                  assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"))
        sync_fn.grant_invoke(scheduler_role)
        auth_fn.grant_invoke(scheduler_role)
        for cid, name, expr, fn, payload in [
            ("SyncSchedule", "spotify-sync", sync_schedule, sync_fn, "{}"),
            # once a day, ignore the "anything new?" shortcut and re-check every playlist
            ("FullCheckSchedule", "spotify-sync-full", full_check_schedule, sync_fn, '{"full":true}'),
            ("ReminderSchedule", "spotify-sync-reminder", reminder_schedule, auth_fn, '{"action":"remind"}'),
            ("PlaybackSchedule", "spotify-sync-playback", playback_schedule, sync_fn, '{"playback":true}'),
            ("PlaybackFinalSchedule", "spotify-sync-playback-final", playback_final_schedule, sync_fn,
             '{"playback":true,"final":true}'),
        ]:
            sched = scheduler.CfnSchedule(
                self, cid, name=name, schedule_expression=expr, schedule_expression_timezone=timezone,
                flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
                target=scheduler.CfnSchedule.TargetProperty(
                    arn=fn.function_arn, role_arn=scheduler_role.role_arn, input=payload,
                    retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(maximum_retry_attempts=0)))
            # Scheduler checks the role when the schedule is created: wait for its policy.
            sched.node.add_dependency(scheduler_role)

        # --- email if the sync keeps failing (10+ failed runs an hour, 3 hours in a row);
        #     the odd transient Spotify error at one run per minute won't trigger it
        alarm = cw.Alarm(
            self, "SyncFailing", alarm_name="spotify-sync-failing",
            metric=sync_fn.metric_errors(period=Duration.hours(1), statistic="Sum"),
            threshold=10, evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING)
        alarm.add_alarm_action(cw_actions.SnsAction(topic))

        CfnOutput(self, "AuthUrl", value=url.url, description="Public URL of spotify-sync-auth")
        CfnOutput(self, "SpotifyRedirectUri", value=f"{url.url}callback",
                  description="Add this to your Spotify app's Redirect URIs (once)")
        CfnOutput(self, "GetApprovalLink",
                  value="aws lambda invoke --function-name spotify-sync-auth "
                        "--cli-binary-format raw-in-base64-out --payload file://cdk/link.json link-out.json",
                  description="Run from the repo root, then open the approval_link in link-out.json")
