# spotify-sync: notes for Claude Code

Self-hosted replacement for IFTTT's Spotify applets. The owner (GitHub: cbyeh) runs it on AWS Lambda in
**us-west-2**. Read README.md and aws/README.md for user-facing docs. This file covers what you need to
work on it safely.

## Layout

- `spotify_sync.py`: all sync logic plus a CLI (`auth`, `playlists`, `run [--dry-run] [--playback]`). Standard library only.
- `config.toml`: the owner's real jobs. It's bundled into the Lambda, so a config change needs a code redeploy.
- `aws/lambda_function.py`: the sync Lambda entry point. Secrets come from SSM. It keeps per-job "last seen"
  markers in SSM under `/spotify-sync/state/`, plus an overlap lock at `/spotify-sync/lock`.
- `aws/auth_function.py`: the approval/renewal Lambda behind a public function URL (`/start?k=<link key>`,
  `/callback`, daily `{"action":"remind"}`, `{"action":"link"}`).
- `cdk/`: TypeScript CDK app (`app.ts`, `spotify-sync-stack.ts`; `npm ci`, then `cdk ...` or `npx cdk ...`) describing
  the same infrastructure. Ported from Python on Nov 24; all 5 synth modes (default, no alertEmail, smoke, importCache,
  importCache+smoke) gave templates identical to the Python app's, logical IDs and asset hash included.
  **This is the live deployment** (stack `SpotifySync`, since Nov 24 2026). Change things with `cdk deploy` only.
- `aws/spend_cap_function.py`: account-wide spend-cap kill switch (see "Spend cap" below). Not part of the CDK stack.
- `tests/`: dependency-free tests using fakes. Run `python tests/test_sync_logic.py`, `python tests/test_playback.py`,
  `python tests/test_cache.py` and `python tests/test_spend_cap.py`.
- `.github/workflows/sync.yml`: alternative hourly runner on GitHub Actions. It skips when secrets aren't set,
  which they aren't for the owner.

## Live deployment (CDK stack `SpotifySync`)

Migrated from the hand-built setup on Tue Nov 24 2026, 5:02–5:07 PM PT (sync down ~5 minutes), following
cdk/MIGRATION.md with the cache bucket adopted by `cdk import` (step 3b). The hand-built backup is in
`~/spotify-sync-backup` on the owner's Windows machine (outside the repo). Deploy from `cdk/`:
`cdk deploy -c alertEmail=<the owner's address, see the backup's topic-subscriptions.json>`; **always pass the same
alertEmail**, or the subscription is removed. Use `-c schedules=off` + a manual `{"dry_run": true}` before switching
the schedules back on after risky changes. The CDK account is bootstrapped (`CDKToolkit`). All in us-west-2:
- Lambdas `spotify-sync` (python3.13, arm64, 128 MB, 300 s; env `PARAM_PREFIX`, `TOPIC_ARN`, `CACHE_BUCKET`) and `spotify-sync-auth`
  (120 s; same env). Both use role `spotify-sync-lambda`. Handlers `aws.lambda_function.handler` / `aws.auth_function.handler`.
  `spotify-sync-auth` reads its public URL from the stack-owned `/spotify-sync/auth-url` (no `AUTH_BASE_URL` any more).
- Schedules (EventBridge Scheduler, role `spotify-sync-scheduler`, timezone America/Los_Angeles):
  `spotify-sync` rate(1 minute) · `spotify-sync-full` 3:30 AM daily `{"full":true}` · `spotify-sync-reminder` 9 AM daily
  → auth `{"action":"remind"}` · `spotify-sync-playback` Mon 4 AM `{"playback":true}` ·
  `spotify-sync-playback-final` Mon 5 AM `{"playback":true,"final":true}`.
- SNS topic `spotify-sync-alerts` (recreated by the stack: the owner must re-confirm the email subscription),
  alarm `spotify-sync-failing`, 14-day log groups (the pre-migration logs were deleted with the old groups).
- S3 bucket `spotify-sync-cache-<account-id>-us-west-2` (private, Block Public Access, SSE-S3): `cache/<playlist>.json.gz`
  holds each destination's snapshot_id + track URIs, so cold starts and the daily full check don't re-read big playlists.
  It's safe to delete (it's only a cache). The role may only Get/Put/Delete `cache/*`, plus ListBucket limited to the
  `cache/` prefix (without it, S3 reports a missing key as AccessDenied instead of NoSuchKey).
- SSM `/spotify-sync/*`: `client-id`, `client-secret`, `refresh-token`, `refresh-token-issued`, `link-key`, `user-id`,
  `last-reminded` (SecureString or String), and `state/*`. **These are the only copy of the secrets. Never delete or
  print them.**
- **Spend cap (account-wide, covers litonwalls too).** Budget `spend-cap-2usd` ($2/month, actual): emails at 50% and
  forecast 100%; at actual 100% it emails and publishes to SNS `spend-cap`, which invokes Lambda `spend-cap-killswitch`
  (role `spend-cap-killswitch`, source `aws/spend_cap_function.py`). The kill switch, in us-west-2, disables every enabled
  schedule, sets reserved concurrency 0 on every other Lambda, and turns Block Public Access fully on for every bucket.
  It deletes nothing and records what it changed in SSM `/spend-cap/frozen`. Undo: invoke it with `{"action":"restore"}`
  (raise the budget first if still over). Preview: `{"action":"freeze","dry_run":true}`. **Don't publish to the
  `spend-cap` topic to test it: any SNS message is a real freeze.** The CDK migration deletes and recreates the
  spotify-sync schedules/Lambdas; the kill switch lists them at freeze time, so no change is needed there. The older
  `My Monthly Cost Budget` ($3, email-only) is the owner's and was left as is.
- Code changes deploy with `cdk deploy` (the code package is `spotify_sync.py`, `config.toml`,
  `aws/lambda_function.py`, `aws/auth_function.py`; don't use `update-function-code` any more, it would drift).
- Leftover log groups from earlier helper Lambdas (`/aws/lambda/spotify-sync-builder-tmp`, `-diag-tmp`,
  `-s3check-tmp`) aren't part of anything; safe to delete when convenient.

## Spotify API constraints (learned the hard way)

- **Development Mode** (Feb 2026 rules): the app owner needs Premium. Playlist track endpoints are `/playlists/{id}/items`
  (renamed from `/tracks`), and items are returned only for playlists the user owns or collaborates on.
  Spotify-generated playlists (Discover Weekly etc.) return 404 for reading.
- **Refresh tokens expire 6 months after approval**, and refreshing doesn't extend them. The auth Lambda emails a renewal link from day 150.
- **Recently played** only records tracks that play to the end. Each play's `context.uri` says which playlist it came
  from, which is how `only_from` archives the Discover Weekly songs the owner plays.
- **Quota (429 `QUOTA_EXCEEDED`)**: Development Mode has a **daily** quota per developer account, and it's tracked
  separately per endpoint family. On Nov 23 2026, reading playlists (`/playlists/{id}/items`) hit it with
  `Retry-After` ≈ 20 h, while recently-played still worked. The cause was repeated full reads of All Weeklies
  (~7.5k tracks = 75 pages each) plus diagnostics. The sync now raises `QuotaExceeded` when `Retry-After` > 120 s,
  and the Lambda writes `/spotify-sync/state/backoff-until` and skips every run until then. **Never page through
  big playlists in diagnostics.**
- Some player commands return non-JSON bodies, and `Spotify.request` tolerates that.

## `via = "playback"` (full Discover Weekly copy): read before touching

It briefly plays Discover Weekly at volume 0 on the owner's **Yamaha RX-A2A receiver ("Maison Yee")** to read the
tracks, then restores the player. Hard-won rules, each covered by tests in `tests/test_playback.py`:
- Never act if something is playing. If Spotify says "playing" but `progress_ms` is frozen, the client is out of
  sync, so skip as well (stale state would restore the wrong settings).
- With no active session, the transfer to the device is asynchronous. Wait until `/me/player` reports that device
  before saving state, or abort without changing anything.
- Discover Weekly keeps serving tracks **past position 29 while still reporting it as the context**, so the end
  can't be detected. Read exactly `take = 30`.
- Restoring races: a pause can land before the reload, and loading a context resets shuffle/repeat. The restore
  re-reads and corrects in a loop. **The volume is restored only once confirmed paused; otherwise it stays muted.**
- Phones are never used (they can't be muted remotely). Spotify's API can't power the receiver off or restore its
  input; it stays on until its own auto-standby.
- Runs once per ISO week (marker in SSM state); the 5 AM attempt emails if neither attempt could run.

## Open items

0. ~~Nov 23 Discover Weekly retry~~: done Tue Nov 24, 2 PM (30 read, 17 new; week marked done).

1. ~~CDK migration~~: **done** Nov 24 (above). Dry run and full dry run on the new function were clean (the full
   one read All Weeklies from the S3 cache in ~3 s), then scheduled runs resumed. **Owner to do:** confirm the new
   SNS subscription email; add the new `/callback` redirect URI (`SpotifySync.SpotifyRedirectUri` stack output) in
   the Spotify developer dashboard, then remove the old one; optionally open the new approval link (in the backup
   folder's `link-out.json`, never commit it) to check it lands on "All set ✅". Until the redirect URI is added,
   renewal links fail at Spotify; the sync itself is unaffected (the refresh token stays in SSM).
2. ~~Reduce playlist reads~~: **done** (S3 cache above; a playlist is re-read only when its snapshot_id changed or
   weekly as a safety net). The `cdk-parity` PR includes the bucket, its permissions and `CACHE_BUCKET`;
   MIGRATION.md step 3b adopts the hand-made bucket and its policy with `cdk import -c importCache=true`
   (tested on a smoke copy Nov 24: the contents survived the import and the full deploy), so the cache isn't lost.
3. **Pre-existing duplicates** (from IFTTT, found Nov 24 from the S3 cache, no Spotify reads): All Weeklies has 154
   extra copies of 132 tracks (latest at position 5050 of 7532), Liked Songs backup 19. None came from this tool.
   The owner was offered removal (keep first occurrence) and hasn't decided: ask before removing anything.
4. Verify the first real full Discover Weekly run on **Mon Nov 30, 2026, 4 AM PT** (logs: `aws logs tail /aws/lambda/spotify-sync`).
5. Security housekeeping: rotate the Spotify client secret (it was pasted in a chat), and move the AWS MCP/CLI off
   the root user to a scoped IAM user.
6. litonwalls (separate repo, same account): bucket `litonwallz` has Block Public Access off and posted images are
   public-read; its Instagram token is hardcoded. Suggested fix: presigned URLs for Instagram, then re-enable Block
   Public Access, and move the token to SSM.

## Conventions

- Commit as the owner (`cbyeh`) and end commit messages with the session's Co-Authored-By trailer.
- Test with fakes before any live call, and do live changes as a dry run first (`{"dry_run": true}`).
- The repo is public: never commit secrets, the account ID, or the approval link.
