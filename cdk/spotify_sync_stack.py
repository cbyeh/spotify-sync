"""CDK stack for spotify-sync: everything deploy.ps1 used to create, as one CloudFormation stack.

Secrets are NOT managed here. You create <prefix>/client-id and <prefix>/client-secret yourself
(CloudFormation can't create SecureString parameters, and secrets don't belong in templates).
The functions write the rest (refresh-token, link-key, ...) at runtime.
"""
import os

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_scheduler as scheduler,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_ssm as ssm,
)
from constructs import Construct

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Everything in the repo except what the Lambdas need: spotify_sync.py, config.toml, aws/*_function.py
ASSET_EXCLUDE = [
    ".git", ".github", ".venv", "cdk", "cdk.out", "tests", "**/__pycache__",
    "*.md", "*.json", "aws/*.ps1", "aws/build", "aws/*.zip", ".gitignore",
]


class SpotifySyncStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *,
                 param_prefix: str = "/spotify-sync",
                 alert_email: str | None = None,
                 sync_schedule: str = "rate(1 hour)",
                 reminder_schedule: str = "cron(0 17 * * ? *)",
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        prefix = "/" + param_prefix.strip("/")

        code = lambda_.Code.from_asset(REPO_ROOT, exclude=ASSET_EXCLUDE)

        # --- notifications: renewal reminders + failure alerts
        topic = sns.Topic(self, "Alerts", topic_name="spotify-sync-alerts")
        if alert_email:
            topic.add_subscription(subs.EmailSubscription(alert_email))

        params_arn = self.format_arn(service="ssm", resource="parameter",
                                     resource_name=prefix.lstrip("/") + "/*")

        def make_function(cid: str, name: str, handler: str, env: dict) -> lambda_.Function:
            log_group = logs.LogGroup(
                self, f"{cid}Logs", log_group_name=f"/aws/lambda/{name}",
                retention=logs.RetentionDays.TWO_WEEKS, removal_policy=RemovalPolicy.DESTROY)
            fn = lambda_.Function(
                self, cid, function_name=name,
                runtime=lambda_.Runtime.PYTHON_3_13, architecture=lambda_.Architecture.ARM_64,
                handler=handler, code=code, memory_size=128, timeout=Duration.minutes(2),
                environment=env, log_group=log_group)
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:PutParameter"],
                resources=[params_arn]))
            return fn

        sync_fn = make_function("SyncFunction", "spotify-sync", "aws.lambda_function.handler",
                                {"PARAM_PREFIX": prefix})
        auth_fn = make_function("AuthFunction", "spotify-sync-auth", "aws.auth_function.handler",
                                {"PARAM_PREFIX": prefix, "TOPIC_ARN": topic.topic_arn})
        topic.grant_publish(auth_fn)

        # --- public approval URL (guarded in code by the secret link key)
        url = auth_fn.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)
        # Since Oct 2025 a public function URL also needs lambda:InvokeFunction scoped to URL calls.
        # Set via override so this works whatever aws-cdk-lib version generated the L1 class.
        invoke_via_url = lambda_.CfnPermission(
            self, "AuthInvokeViaUrl", action="lambda:InvokeFunction",
            function_name=auth_fn.function_name, principal="*")
        invoke_via_url.add_property_override("InvokedViaFunctionUrl", True)
        # The function can't reference its own URL (circular), so it reads it from SSM when needed.
        ssm.StringParameter(self, "AuthUrlParam", parameter_name=f"{prefix}/auth-url",
                            string_value=url.url)

        # --- schedules (EventBridge Scheduler)
        scheduler_role = iam.Role(self, "SchedulerRole",
                                  assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"))
        sync_fn.grant_invoke(scheduler_role)
        auth_fn.grant_invoke(scheduler_role)
        for cid, name, expr, fn, payload in [
            ("SyncSchedule", "spotify-sync", sync_schedule, sync_fn, "{}"),
            ("ReminderSchedule", "spotify-sync-reminder", reminder_schedule, auth_fn, '{"action":"remind"}'),
        ]:
            scheduler.CfnSchedule(
                self, cid, name=name, schedule_expression=expr,
                flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
                target=scheduler.CfnSchedule.TargetProperty(
                    arn=fn.function_arn, role_arn=scheduler_role.role_arn, input=payload,
                    retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(maximum_retry_attempts=0)))

        # --- email if the sync fails 3 hours in a row
        if alert_email:
            alarm = cw.Alarm(
                self, "SyncFailing", alarm_name="spotify-sync-failing",
                metric=sync_fn.metric_errors(period=Duration.hours(1), statistic="Sum"),
                threshold=1, evaluation_periods=3,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING)
            alarm.add_alarm_action(cw_actions.SnsAction(topic))

        CfnOutput(self, "SpotifyRedirectUri", value=f"{url.url}callback",
                  description="Add this to your Spotify app's Redirect URIs (once)")
        CfnOutput(self, "GetApprovalLink",
                  value="aws lambda invoke --function-name spotify-sync-auth "
                        "--cli-binary-format raw-in-base64-out --payload file://cdk/link.json link-out.json",
                  description="Run from the repo root, then open the approval_link in link-out.json")
