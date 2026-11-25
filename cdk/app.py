#!/usr/bin/env python3
import os

import aws_cdk as cdk

from spotify_sync_stack import SpotifySyncStack

def flag(value) -> bool:
    return str(value or "").lower() in ("1", "true", "yes")


app = cdk.App()
ctx = app.node.try_get_context

# `-c importCache=true` synthesizes only the playlist cache bucket and its policy, for `cdk import`
# (MIGRATION.md step 3b). Run it with --no-version-reporting: CDK's AWS::CDK::Metadata resource
# can't be imported.
import_cache = flag(ctx("importCache"))

# `-c smoke=true` deploys a throwaway copy for testing the stack itself (cdk/MIGRATION.md, step 2b):
# every name gets a "-smoke" suffix, it reads its own SSM prefix (never the real Spotify secrets,
# whose refresh token a second copy could rotate out from under the live one), its schedules are
# created switched off, and it sends no email. It doesn't touch the live deployment.
smoke = flag(ctx("smoke"))

SpotifySyncStack(
    app, "SpotifySyncSmoke" if smoke else "SpotifySync",
    name="spotify-sync-smoke" if smoke else "spotify-sync",
    schedules_enabled=not smoke,
    cache_only=import_cache,
    alert_email=None if smoke else (ctx("alertEmail") or None),
    param_prefix="/spotify-sync-smoke" if smoke else (ctx("paramPrefix") or "/spotify-sync"),
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
