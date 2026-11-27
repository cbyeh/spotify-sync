# spotify-sync

A free, self-hosted replacement for IFTTT's Spotify "new track → add to playlist" applets.
It copies new tracks from a source into a destination playlist: every 5 minutes on AWS Lambda, or hourly on GitHub Actions.

| Source | Works? |
|---|---|
| Liked Songs → backup playlist | ✅ |
| Recently played → rolling "last N tracks" playlist, or a permanent listening archive | ✅ |
| A playlist you own or collaborate on → another playlist | ✅ |
| Discover Weekly / Release Radar → archive, **songs you play from them** | ✅ via `only_from` (see below) |
| The *full* Discover Weekly list, including songs you haven't played | ✅ opt-in, `via = "playback"` (see below) |

There are two ways to run it, and both are free: **GitHub Actions** (set up below) or **AWS Lambda**, deployed with CDK ([aws/README.md](aws/README.md)).
The AWS version also emails you a one-tap renewal link before Spotify's 6-month token expiry.
Pick one. If you run both, nothing gets duplicated; it's just redundant.

It's stateless: before adding, it reads the destination playlist and skips anything already there.
Missed runs, re-runs and manual runs never create duplicates.

## Setup (about 10 minutes, once per person)

Each person uses their own Spotify developer app. Spotify requires the app owner to have **Premium**.

1. **Create a Spotify app.** At <https://developer.spotify.com/dashboard>, click *Create app*.
   - Redirect URI: `http://127.0.0.1:8888/callback` (use exactly this; Spotify no longer accepts `localhost`)
   - API: *Web API*
   - Copy the **Client ID** and **Client secret**.
2. **Get a refresh token** on your own computer (Python 3.11+, nothing to install):
   ```sh
   export SPOTIFY_CLIENT_ID=...  SPOTIFY_CLIENT_SECRET=...
   python spotify_sync.py auth
   ```
   Log in in the browser, then copy the token it prints.
3. **Fork or copy this repo** (private is fine). Under *Settings → Secrets and variables → Actions*, add
   `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` and `SPOTIFY_REFRESH_TOKEN`.
4. **Edit `config.toml`**: paste the link of your backup playlist (Share → Copy link to playlist).
   To list your playlist IDs: `SPOTIFY_REFRESH_TOKEN=... python spotify_sync.py playlists`.
5. **Test it**: *Actions → Spotify sync → Run workflow* with *Dry run* checked, then run it for real.
   After that it runs every hour.

To copy your entire Liked Songs history on the first run, set `backfill = true` for that job, run once, then set it back.

## Recently played

`from = "recent"` reads your last 50 plays, which is all Spotify returns. Pick a `mode`:
- **`mode = "rolling"`** (with `keep = 100`, the default): the playlist always holds your last 100 *distinct* tracks, newest first.
  A song you replay moves back to the top instead of showing up twice.
  The playlist can hold more than 50 because each run merges the newest plays into what's already there.
- **`mode = "archive"`**: a permanent listening history. Every distinct track you play gets added once, oldest first, and is never removed.
- **`mode = "last_played"`**: every song you've ever played, with no limit, ordered by when you *last* played it (most recent at the bottom).
  A replayed song is removed and added again at the end, so it never appears twice and Spotify's "Date added" shows its last play.

You can run both at once: add two `recent` jobs that point at different playlists.

On AWS, changes show up within about 5 minutes; on GitHub Actions, within an hour (it catches everything unless you finish more than 50 tracks between runs).

### Listening-history log (AWS only)

The AWS stack also keeps every finished play as a small JSON file in a private, versioned S3 bucket
(`spotify-sync-history-<account>-<region>`, key `plays/YYYY/MM/DD/<played_at>.json`): when you played it, the
track, artists, album, length, and which playlist or album it was played from. It isn't a playlist, it's data
for your own stats. It reuses the plays each run already fetches, so it costs no extra Spotify requests, and
it's kept when the stack is destroyed. Like everything here it only sees your last 50 plays per run; if more
than that pile up between runs (e.g. during a quota pause), the log warns about the possible gap. To fill in
older history, request your *Extended streaming history* in Spotify's privacy settings.
In practice, Spotify only records a song in this history once it plays through to the end. Songs you skip partway are left out,
even after 30+ seconds, and offline plays are sometimes missed.

### Archiving Discover Weekly (the songs you play)

Personal apps can't read Discover Weekly, but your listening history records which playlist each song was played from.
`only_from` keeps only those plays:

```toml
[[sync]]
name = "Discover Weekly -> All Weeklies"
from = "recent"
only_from = "https://open.spotify.com/playlist/37i9dQZEVXc..."   # your Discover Weekly: Share > Copy link
to   = "https://open.spotify.com/playlist/..."                 # your archive playlist
mode = "archive"
```

Every Discover Weekly song you play to the end lands in your archive within about a minute.
The same works for Release Radar, Daily Mixes or any other playlist you listen from.

### The full Discover Weekly list (`via = "playback"`, opt-in, AWS)

Spotify won't let personal apps *read* Discover Weekly, but it does let them *play* it. A `via = "playback"` job reads the
playlist by briefly playing it, then puts everything back:

```toml
[[sync]]
name = "Discover Weekly full list -> All Weeklies"
from = "https://open.spotify.com/playlist/37i9dQZEVXc..."   # your Discover Weekly
to   = "https://open.spotify.com/playlist/..."              # your archive
via  = "playback"
# device = "My PC"   # optional: a specific Spotify device (default: any computer)
```

What happens every Monday at 4 AM (your time zone, `timezone` in `cdk/cdk.json`):

1. It skips the attempt if anything is playing, or if no Spotify **desktop** app is open and idle. Phones are never used,
   because they can't be muted remotely.
2. It saves your device, shuffle, repeat, volume and what you had loaded (track and position), then mutes and turns shuffle off.
3. It plays the playlist at a few positions for about a second each, reading the queue and verifying each track against
   the position it plays from. This also finds where the playlist ends and autoplay begins.
   A 30-track Discover Weekly takes about 10 brief starts, each well under the 30 seconds that counts as a stream.
4. It pauses and restores everything: what was loaded and where, shuffle, repeat, volume and the previously active device.
   Smart Shuffle isn't exposed by the API, so it's left alone.

If it can't run at 4 AM (the device isn't available, or something is playing), it tries once more at 5 AM.
If that fails too, it emails you so you can copy the list by hand. For the receiver or desktop app to be
reachable, it needs to stay visible to Spotify: network standby on a receiver, or the app left open on a computer.

Caveats: it needs the playback permissions (`user-read-playback-state`, `user-modify-playback-state`), so existing
setups must re-approve once. Spotify may treat the brief starts as skips; it's a handful per week, but the effect on
recommendations is unknown.

## Playlist IDs vs names

Use the ID. Every playlist has a fixed 22-character ID (the part after `/playlist/` in its share link). Names aren't unique and can change, but the ID stays the same.
`config.toml` takes a share link, a `spotify:playlist:` URI or a bare ID.

## Why the full Discover Weekly list can't be copied

Since 27 Nov 2024, apps without Spotify's "extended quota" status can't read Spotify-generated playlists. Spotify no longer grants that status to individuals.
In February 2026, reading a playlist's tracks was also limited to playlists you own or collaborate on.
IFTTT still has that access because it is a large commercial partner.

Workarounds:
- **`only_from` (automatic):** archives every Discover Weekly song you actually play (see above).
- **Manual, for the songs you don't play (10 seconds a week):** open Discover Weekly, select all tracks (Ctrl/Cmd+A on desktop), and choose *Add to playlist* → your archive.
  If you prefer, *Add to Liked Songs* also works, and the liked-songs job then backs them up automatically.
- **Unofficial web-player tokens:** some tools use your browser's `sp_dc` cookie to impersonate the web player.
  This breaks Spotify's developer terms and stops working without warning, so it isn't included here.

## Notes

- **Re-authorize every 6 months.** Spotify refresh tokens expire 6 months after you approve the app, and using them doesn't extend that.
  On AWS, you get an email with a one-tap renewal link about a month before expiry.
  On GitHub Actions, runs fail with a message telling you to re-run `auth`. Run it, then update the `SPOTIFY_REFRESH_TOKEN` secret.

- **Unliking a song** doesn't remove it from the backup. That matches the IFTTT applet.
- **Deleting a track from the destination** means it gets added again if it's still in the source
  (for Liked Songs, only if it's among your recent likes).
- **GitHub pauses scheduled workflows** in public repos after 60 days with no commits. Use a private repo,
  or re-enable the workflow if GitHub emails you.
- **Development Mode limits:** new Spotify apps are capped at 5 authorized users, and the app owner needs Premium.
  That's why each person sets up their own copy instead of sharing one hosted service.
