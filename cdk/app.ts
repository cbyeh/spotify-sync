#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";

import { SpotifySyncStack } from "./spotify-sync-stack";

function flag(value: unknown): boolean {
  return ["1", "true", "yes"].includes(String(value ?? "").toLowerCase());
}

const app = new cdk.App();
const ctx = (key: string): string | undefined => app.node.tryGetContext(key) || undefined;

// `-c importCache=true` synthesizes only the playlist cache bucket and its policy, for `cdk import`
// (MIGRATION.md step 3b). Run it with --no-version-reporting: CDK's AWS::CDK::Metadata resource
// can't be imported.
const importCache = flag(ctx("importCache"));

// `-c smoke=true` deploys a throwaway copy for testing the stack itself (cdk/MIGRATION.md, step 2b):
// every name gets a "-smoke" suffix, it reads its own SSM prefix (never the real Spotify secrets,
// whose refresh token a second copy could rotate out from under the live one), its schedules are
// created switched off, and it sends no email. It doesn't touch the live deployment.
const smoke = flag(ctx("smoke"));

// `-c schedules=off` creates every schedule switched off, so the first run after a deploy can be a
// manual {"dry_run": true}; deploy again without it to switch them on (MIGRATION.md step 5).
const schedulesOff = String(ctx("schedules") ?? "").toLowerCase() === "off";

new SpotifySyncStack(app, smoke ? "SpotifySyncSmoke" : "SpotifySync", {
  name: smoke ? "spotify-sync-smoke" : "spotify-sync",
  schedulesEnabled: !smoke && !schedulesOff,
  cacheOnly: importCache,
  alertEmail: smoke ? undefined : ctx("alertEmail"),
  paramPrefix: smoke ? "/spotify-sync-smoke" : (ctx("paramPrefix") ?? "/spotify-sync"),
  syncSchedule: ctx("syncSchedule") ?? "rate(5 minutes)",
  timezone: ctx("timezone") ?? "America/Los_Angeles",
  // The region is pinned in cdk.json ("region"), not taken from CDK_DEFAULT_REGION: the CDK CLI
  // always sets that from the SDK's region, which falls back to us-east-1 when none is configured,
  // and that would quietly deploy a second copy in the wrong region.
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: ctx("region") ?? "us-west-2",
  },
});

app.synth();
