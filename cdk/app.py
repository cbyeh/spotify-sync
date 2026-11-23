#!/usr/bin/env python3
import os

import aws_cdk as cdk

from spotify_sync_stack import SpotifySyncStack

app = cdk.App()
ctx = app.node.try_get_context

SpotifySyncStack(
    app, "SpotifySync",
    alert_email=ctx("alertEmail") or None,
    param_prefix=ctx("paramPrefix") or "/spotify-sync",
    sync_schedule=ctx("syncSchedule") or "rate(1 hour)",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION") or "us-west-2",
    ),
)

app.synth()
