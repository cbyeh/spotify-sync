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
- `cdk/`: Python CDK app describing the same infrastructure. **Never synthesized or deployed yet** (see below).
- `tests/`: dependency-free tests using fakes. Run `python tests/test_sync_logic.py` and `python tests/test_playback.py`.
- `.github/workflows/sync.yml`: alternative hourly runner on GitHub Actions. It skips when secrets aren't set,
  which they aren't for the owner.

## Live deployment (built by hand, not CDK)

All in us-west-2:
- Lambdas `spotify-sync` (python3.13, arm64, 128 MB, 300 s; env `PARAM_PREFIX`, `TOPIC_ARN`) and `spotify-sync-auth`
  (120 s; env `PARAM_PREFIX`, `TOPIC_ARN`, `AUTH_BASE_URL`). Both use role `spotify-sync-lambda`.
- Schedules (EventBridge Scheduler, role `spotify-sync-scheduler`, timezone America/Los_Angeles):
  `spotify-sync` rate(1 minute) · `spotify-sync-full` 3:30 AM daily `{"full":true}` · `spotify-sync-reminder` 9 AM daily
  → auth `{"action":"remind"}` · `spotify-sync-playback` Mon 4 AM `{"playback":true}` ·
  `spotify-sync-playback-final` Mon 5 AM `{"playback":true,"final":true}`.
- SNS topic `spotify-sync-alerts` (email subscription confirmed), alarm `spotify-sync-failing`, 14-day log groups.
- SSM `/spotify-sync/*`: `client-id`, `client-secret`, `refresh-token`, `refresh-token-issued`, `link-key`, `user-id`,
  `last-reminded` (SecureString or String), and `state/*`. **These are the only copy of the secrets. Never delete or
  print them.**
- How the live code got deployed: the cloud session couldn't upload zips, so a temporary CloudFormation-inline
  "builder" Lambda downloaded a pinned GitHub commit and called UpdateFunctionCode. Locally you can just
  zip `spotify_sync.py config.toml aws/lambda_function.py aws/auth_function.py` **flat** (no folders) and run
  `aws lambda update-function-code --function-name spotify-sync --zip-file fileb://function.zip`
  (same zip for `spotify-sync-auth`). Handlers: `lambda_function.handler` / `auth_function.handler`.

## Next task: move the live deployment onto CDK

A background worker prepared branch **`cdk-parity`** with a PR: the stack checked against CDK docs and matched
to the live setup above, plus `cdk/MIGRATION.md`. Review that PR, then on the owner's machine (Windows, PowerShell):
`cdk synth` → `cdk diff` → follow MIGRATION.md. Key points:
- The stack reuses the same physical names, so the hand-built resources must be deleted first, **except SSM**.
- The CDK asset keeps the repo layout, so handlers are `aws.lambda_function.handler` / `aws.auth_function.handler`.
- The new function URL means adding the new `/callback` redirect URI in the Spotify developer dashboard.
  No re-approval is needed, because the refresh token stays in SSM. The owner re-confirms the SNS email.
- The function URL needs both `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` (InvokedViaFunctionUrl), required since Oct 2025.

## Spotify API constraints (learned the hard way)

- **Development Mode** (Feb 2026 rules): the app owner needs Premium. Playlist track endpoints are `/playlists/{id}/items`
  (renamed from `/tracks`), and items are returned only for playlists the user owns or collaborates on.
  Spotify-generated playlists (Discover Weekly etc.) return 404 for reading.
- **Refresh tokens expire 6 months after approval**, and refreshing doesn't extend them. The auth Lambda emails a renewal link from day 150.
- **Recently played** only records tracks that play to the end. Each play's `context.uri` says which playlist it came
  from, which is how `only_from` archives the Discover Weekly songs the owner plays.
- **Rate limits (429)**: everything shares one quota per developer account, including the every-minute sync. Don't burst
  through big playlists in diagnostics (All Weeklies has ~7.5k tracks = 75 pages). Throttle and honor `Retry-After`.
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

1. Review and merge the `cdk-parity` PR, then do the migration (above).
2. Check All Weeklies (`41JXOe9PZ9g6v2PUW687l6`) for **pre-existing duplicates** from before this tool. The sync itself
   never adds one (it dedupes by track URI against the whole playlist), but the first scan attempt hit a 429.
   Re-run slowly, report exact-URI duplicates and same-name/artist pairs, and ask the owner before removing anything.
3. Verify the first real full Discover Weekly run on **Mon Nov 30, 2026, 4 AM PT** (logs: `aws logs tail /aws/lambda/spotify-sync`).
4. Security housekeeping: rotate the Spotify client secret (it was pasted in a chat), and move the AWS MCP/CLI off
   the root user to a scoped IAM user.
5. litonwalls (separate repo, same account): bucket `litonwallz` has Block Public Access off and posted images are
   public-read; its Instagram token is hardcoded. Suggested fix: presigned URLs for Instagram, then re-enable Block
   Public Access, and move the token to SSM.

## Conventions

- Commit as the owner (`cbyeh`) and end commit messages with the session's Co-Authored-By trailer.
- Test with fakes before any live call, and do live changes as a dry run first (`{"dry_run": true}`).
- The repo is public: never commit secrets, the account ID, or the approval link.
