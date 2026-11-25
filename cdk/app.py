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
    sync_schedule=ctx("syncSchedule") or "rate(1 minute)",
    timezone=ctx("timezone") or "America/Los_Angeles",
    # The region is pinned in cdk.json ("region"), not taken from CDK_DEFAULT_REGION: the CDK CLI
    # always sets that from the SDK's region, which falls back to us-east-1 when none is configured,
    # and that would quietly deploy a second copy in the wrong region.
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=ctx("region") or "us-west-2",
    ),
)

app.synth()
