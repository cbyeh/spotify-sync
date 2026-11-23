# spotify-sync

A free, self-hosted replacement for IFTTT's Spotify "new track → add to playlist" applets.
It runs hourly on GitHub Actions and copies new tracks from a source into a destination playlist.

| Source | Works? |
|---|---|
| Liked Songs → backup playlist | ✅ |
| Recently played → rolling "last N tracks" playlist, or a permanent listening archive | ✅ |
| A playlist you own or collaborate on → another playlist | ✅ |
| Discover Weekly, Release Radar, Daily Mix, other Spotify-made playlists | ❌ Spotify blocks these for personal apps (see below) |

There are two ways to run it, and both are free: **GitHub Actions** (set up below) or **AWS Lambda**, deployed with CDK ([aws/README.md](aws/README.md)).
The AWS version also emails you a one-tap renewal link before Spotify's 6-month token expiry, and needs no Python on your computer.
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

`from = "recent"` reads your last 50 plays, which is all Spotify returns.
- **With `keep = 100`**: the playlist always holds your last 100 *distinct* tracks, newest first. A song you replay moves back to the top instead of showing up twice.
  The playlist can hold more than 50 because each run merges the newest plays into what's already there.
- **Without `keep`**: an append-only archive. Every distinct track you play gets added once, oldest first, and is never removed.

Hourly runs catch everything unless you play more than 50 tracks in an hour.
Spotify's history leaves out skipped songs and only counts tracks you actually listened to, and it sometimes misses offline plays.

## Playlist IDs vs names

Use the ID. Every playlist has a fixed 22-character ID (the part after `/playlist/` in its share link). Names aren't unique and can change, but the ID stays the same.
`config.toml` takes a share link, a `spotify:playlist:` URI or a bare ID.

## Why Discover Weekly doesn't work

Since 27 Nov 2024, apps without Spotify's "extended quota" status can't read Spotify-generated playlists. Spotify no longer grants that status to individuals.
In February 2026, reading a playlist's tracks was also limited to playlists you own or collaborate on.
IFTTT still has that access because it is a large commercial partner.

Workarounds:
- **Manual (10 seconds a week):** open Discover Weekly, select all tracks (Ctrl/Cmd+A on desktop), and choose *Add to playlist* → your archive.
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
