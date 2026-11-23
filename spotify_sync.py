#!/usr/bin/env python3
"""
spotify-sync: a self-hosted replacement for IFTTT's Spotify "copy new tracks" applets.

Copies tracks from a source (your Liked Songs, or a playlist you own/collaborate on)
into a destination playlist, skipping anything already there. Stateless and idempotent:
the destination playlist itself is the record of what has been copied, so you can run
this as often as you like.

Standard library only (Python 3.11+).

Commands:
  python spotify_sync.py auth       one-time: log in and print your refresh token
  python spotify_sync.py playlists  list your playlists with their IDs
  python spotify_sync.py run        run every [[sync]] job in config.toml
  python spotify_sync.py run --dry-run

Sources (`from` in config.toml): "liked", "recent", or a playlist link/ID you own.

Environment variables:
  SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET   from your app at developer.spotify.com
  SPOTIFY_REFRESH_TOKEN                      printed by `auth` (needed for playlists/run)
  SPOTIFY_REDIRECT_URI                       optional, default http://127.0.0.1:8888/callback
"""
from __future__ import annotations

import argparse
import base64
import http.server
import json
import os
import re
import secrets
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Iterator

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = " ".join([
    "user-library-read",             # read Liked Songs
    "user-read-recently-played",     # read listening history (last 50 plays)
    "playlist-read-private",         # read your private playlists
    "playlist-read-collaborative",   # read collaborative playlists
    "playlist-modify-private",       # add to private playlists
    "playlist-modify-public",        # add to public playlists
])

MADE_FOR_YOU_HINT = (
    "Spotify only returns the tracks of playlists you own or collaborate on, and since "
    "Nov 27 2024 personal (Development Mode) apps cannot read Spotify-generated playlists "
    "such as Discover Weekly, Release Radar or Daily Mix at all."
)


class SpotifyError(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def env(name: str, required: bool = True) -> str | None:
    val = os.environ.get(name, "").strip()
    if required and not val:
        sys.exit(f"Missing environment variable {name}. See README.md.")
    return val or None


def parse_playlist_id(value: str) -> str:
    """Accept a share link, a spotify:playlist: URI, or a bare 22-character ID."""
    value = value.strip()
    m = re.search(r"playlist[/:]([A-Za-z0-9]{22})", value) or re.fullmatch(r"([A-Za-z0-9]{22})", value)
    if not m:
        raise ValueError(f"Not a Spotify playlist link, URI or ID: {value!r}")
    return m.group(1)


def basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def token_request(form: dict, client_id: str, client_secret: str) -> dict:
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "Authorization": basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if "invalid_grant" in body and form.get("grant_type") == "refresh_token":
            raise SpotifyError(
                "Spotify refresh token expired or was revoked. Tokens last 6 months from when you "
                "approved the app, and refreshing doesn't extend that. Re-run `python spotify_sync.py auth` "
                "and update the stored token (GitHub secret or SSM parameter).") from None
        raise SpotifyError(f"Token request failed ({e.code}): {body}") from None


def track_uri(entry: dict) -> str | None:
    """Pull a track URI out of a playlist/library entry, handling both the old
    (`track`) and Feb-2026 (`item`) field names. Skips episodes and local files."""
    obj = entry.get("item") or entry.get("track")
    if not obj or obj.get("type") != "track" or obj.get("is_local"):
        return None
    return obj.get("uri")


# --------------------------------------------------------------------------- API client

class Spotify:
    def __init__(self, access_token: str, expires_in: int = 3600):
        self.token = access_token
        self.expires_at = time.time() + expires_in - 120   # refresh a bit early
        self._recent: list[dict] | None = None
        # playlist id -> (snapshot_id, track uris). Survives between runs while the session is
        # reused, so a big destination is only re-read when the playlist actually changed.
        self._playlists: dict[str, tuple[str, list[str]]] = {}

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def new_run(self) -> None:
        """Forget per-run caches (the session itself can be reused while the token is valid)."""
        self._recent = None

    @classmethod
    def from_env(cls, on_new_refresh_token=None) -> "Spotify":
        """on_new_refresh_token(token) is called if Spotify rotates the refresh token,
        so a host that can store secrets (e.g. AWS SSM) can save the new one."""
        cid, secret = env("SPOTIFY_CLIENT_ID"), env("SPOTIFY_CLIENT_SECRET")
        refresh = env("SPOTIFY_REFRESH_TOKEN")
        data = token_request({"grant_type": "refresh_token", "refresh_token": refresh}, cid, secret)
        new = data.get("refresh_token")
        if new and new != refresh:
            if on_new_refresh_token:
                on_new_refresh_token(new)
                print("Spotify issued a new refresh token; saved it.")
            else:
                print("::warning::Spotify issued a new refresh token. The old one usually keeps working, "
                      "but if runs start failing with invalid_grant, re-run `auth` and update the secret.")
        return cls(data["access_token"], int(data.get("expires_in", 3600)))

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = path if path.startswith("http") else API + path
        payload = json.dumps(body).encode() if body is not None else None
        for attempt in range(5):
            req = urllib.request.Request(url, data=payload, method=method, headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    wait = int(e.headers.get("Retry-After", 2 ** attempt))
                    time.sleep(min(wait, 60))
                    continue
                detail = e.read().decode(errors="replace")
                raise SpotifyError(f"{method} {url} -> {e.code}: {detail}") from None
        raise SpotifyError(f"{method} {url} kept failing after retries")

    def pages(self, path: str) -> Iterator[list[dict]]:
        url: str | None = path
        while url:
            data = self.request("GET", url)
            yield data.get("items", [])
            url = data.get("next")

    # -- reads
    def playlist_snapshot(self, playlist_id: str) -> str | None:
        return self.request("GET", f"/playlists/{playlist_id}?fields=snapshot_id").get("snapshot_id")

    def playlist_uris(self, playlist_id: str) -> list[str]:
        cache = getattr(self, "_playlists", None)
        snapshot = self.playlist_snapshot(playlist_id) if cache is not None else None
        if snapshot and cache.get(playlist_id, ("",))[0] == snapshot:
            return list(cache[playlist_id][1])
        uris = []
        for page in self.pages(f"/playlists/{playlist_id}/items?limit=100&additional_types=track"):
            uris += [u for u in map(track_uri, page) if u]
        if snapshot:
            cache[playlist_id] = (snapshot, uris)
        return list(uris)

    def _remember(self, playlist_id: str, snapshot: str | None, uris: list[str]) -> None:
        cache = getattr(self, "_playlists", None)
        if cache is not None:
            if snapshot:
                cache[playlist_id] = (snapshot, list(uris))
            else:
                cache.pop(playlist_id, None)

    def my_playlists(self) -> Iterator[dict]:
        for page in self.pages("/me/playlists?limit=50"):
            yield from page

    def recently_played_items(self) -> list[dict]:
        """Up to 50 most recent plays, newest first (fetched once per run).
        Spotify only records tracks that play through to the end."""
        if self._recent is None:
            self._recent = self.request("GET", "/me/player/recently-played?limit=50").get("items", [])
        return self._recent

    def recently_played(self, only_from: str | None = None) -> list[str]:
        """Track URIs of recent plays, newest first; optionally only those played from one playlist."""
        items = self.recently_played_items()
        if only_from:
            want = f"spotify:playlist:{only_from}"
            items = [i for i in items if (i.get("context") or {}).get("uri") == want]
        return [u for u in map(track_uri, items) if u]

    # -- writes
    def add_to_playlist(self, playlist_id: str, uris: list[str]) -> None:
        cache = getattr(self, "_playlists", {})
        known = cache.get(playlist_id, (None, []))[1]
        snapshot = None
        for i in range(0, len(uris), 100):
            snapshot = self.request("POST", f"/playlists/{playlist_id}/items",
                                    {"uris": uris[i:i + 100]}).get("snapshot_id")
        self._remember(playlist_id, snapshot if playlist_id in cache else None, known + uris)

    def replace_playlist(self, playlist_id: str, uris: list[str]) -> None:
        snapshot = self.request("PUT", f"/playlists/{playlist_id}/items", {"uris": uris[:100]}).get("snapshot_id")
        self._remember(playlist_id, snapshot, uris[:100])
        if len(uris) > 100:
            self.add_to_playlist(playlist_id, uris[100:])


# --------------------------------------------------------------------------- sync logic

def new_from_liked(sp: Spotify, existing: set[str], backfill: bool) -> list[str]:
    """Liked Songs come newest-first. Stop at the first page with nothing new
    (unless backfilling), then return oldest-first so the backup stays chronological."""
    found: list[str] = []
    for page in sp.pages("/me/tracks?limit=50"):
        fresh = [u for u in map(track_uri, page) if u and u not in existing]
        found += fresh
        if not fresh and not backfill:
            break
    found.reverse()
    return found


def new_from_playlist(sp: Spotify, source_id: str, existing: set[str]) -> list[str]:
    try:
        uris = sp.playlist_uris(source_id)
    except SpotifyError as e:
        if any(code in str(e) for code in ("-> 403", "-> 404")):
            raise SpotifyError(f"Can't read source playlist {source_id}. {MADE_FOR_YOU_HINT}") from None
        raise
    return [u for u in uris if u not in existing]


def sync_recent_rolling(sp: Spotify, name: str, dest_id: str, keep: int, dry_run: bool,
                        only_from_id: str | None = None) -> int:
    """Keep the destination as your last `keep` distinct tracks, most recent first.
    A replayed song moves back to the top instead of appearing twice."""
    existing = sp.playlist_uris(dest_id)
    merged = list(dict.fromkeys(sp.recently_played(only_from_id) + existing))[:keep]
    if merged == existing:
        print(f"[{name}] already up to date ({len(existing)} tracks).")
        return 0
    moved = len([u for u in merged if u not in existing])
    verb = "Would update" if dry_run else "Updating"
    print(f"[{name}] {verb} playlist: {moved} new, {len(merged)} tracks total (keep={keep}).")
    if not dry_run:
        sp.replace_playlist(dest_id, merged)
    return moved


def only_from(job: dict) -> str | None:
    """`only_from`: for from = "recent", keep only plays whose source was this playlist
    (e.g. your Discover Weekly). Spotify records the playlist a song was played from."""
    value = job.get("only_from")
    if not value:
        return None
    if str(job["from"]).strip().lower() != "recent":
        raise ValueError('`only_from` only works with from = "recent"')
    return parse_playlist_id(str(value))


def change_marker(sp: Spotify, job: dict) -> str | None:
    """A cheap value that changes whenever the source has something new for this job:
    newest like time, newest (matching) play time, or the source playlist's snapshot id."""
    source = str(job["from"]).strip()
    src = source.lower()
    if src == "liked":
        items = sp.request("GET", "/me/tracks?limit=1").get("items", [])
        return items[0].get("added_at") if items else None
    if src == "recent":
        items = sp.recently_played_items()
        ctx = only_from(job)
        if ctx:
            items = [i for i in items if (i.get("context") or {}).get("uri") == f"spotify:playlist:{ctx}"]
        return items[0].get("played_at") if items else None
    return sp.playlist_snapshot(parse_playlist_id(source))


def state_key(job: dict) -> str:
    source = str(job["from"]).strip()
    src = source.lower() if source.lower() in ("liked", "recent") else parse_playlist_id(source)
    return f"{src}-{parse_playlist_id(job['to'])}"


def run_job(sp: Spotify, job: dict, dry_run: bool, state=None, full: bool = False) -> int:
    """Run one job. With a `state` store (get/set), skip quickly when the source hasn't changed
    since the last successful run, unless `full` is set."""
    if state is None:
        return sync_job(sp, job, dry_run)
    key = state_key(job)
    marker = change_marker(sp, job)
    if not full and marker and state.get(key) == marker:
        return 0
    added = sync_job(sp, job, dry_run)
    if marker and not dry_run:
        state.set(key, marker)
    return added


def sync_job(sp: Spotify, job: dict, dry_run: bool) -> int:
    name = job.get("name", "(unnamed)")
    only_from(job)   # validates the option early
    source = str(job["from"]).strip()
    dest_id = parse_playlist_id(job["to"])
    backfill = bool(job.get("backfill", False))

    if source.lower() == "recent":
        # mode = "rolling" (last N distinct tracks, newest first) or "archive" (every track, kept forever).
        # Older configs without `mode`: `keep` present means rolling, absent means archive.
        mode = str(job.get("mode") or ("rolling" if "keep" in job else "archive")).lower()
        if mode not in ("rolling", "archive"):
            raise ValueError('`mode` must be "rolling" or "archive"')
        if mode == "rolling":
            keep = int(job.get("keep", 100))
            if not 1 <= keep <= 10000:
                raise ValueError("`keep` must be between 1 and 10000")
            return sync_recent_rolling(sp, name, dest_id, keep, dry_run, only_from(job))
        if "keep" in job:
            raise ValueError('`keep` only applies to mode = "rolling"')

    existing = set(sp.playlist_uris(dest_id))
    if source.lower() == "liked":
        to_add = new_from_liked(sp, existing, backfill)
    elif source.lower() == "recent":
        # archive mode: every distinct track you play, appended oldest-first, never removed
        to_add = [u for u in reversed(sp.recently_played(only_from(job))) if u not in existing]
    else:
        src_id = parse_playlist_id(source)
        if src_id == dest_id:
            raise SpotifyError("Source and destination are the same playlist.")
        to_add = new_from_playlist(sp, src_id, existing)

    # de-duplicate within this batch, keeping order
    to_add = list(dict.fromkeys(to_add))
    verb = "Would add" if dry_run else "Adding"
    print(f"[{name}] destination has {len(existing)} tracks; {verb} {len(to_add)} new.")
    if to_add and not dry_run:
        sp.add_to_playlist(dest_id, to_add)
    return len(to_add)


def load_config(path: str) -> list[dict]:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    jobs = cfg.get("sync", [])
    if not jobs:
        sys.exit(f"No [[sync]] entries found in {path}.")
    for j in jobs:
        if "from" not in j or "to" not in j:
            sys.exit(f"Each [[sync]] entry needs `from` and `to`: {j}")
    return [j for j in jobs if j.get("enabled", True)]


# --------------------------------------------------------------------------- commands

def run_all(config_path: str, dry_run: bool = False, on_new_refresh_token=None,
            state=None, full: bool = False, sp: Spotify | None = None) -> list[str]:
    """Run every job; returns a list of error messages (empty means all succeeded).
    Pass a `state` store to enable quick "anything new?" checks, and `sp` to reuse a session."""
    jobs = load_config(config_path)
    if sp is None or sp.expired:
        sp = Spotify.from_env(on_new_refresh_token)
    sp.new_run()
    errors = []
    for job in jobs:
        try:
            run_job(sp, job, dry_run, state, full)
        except (SpotifyError, ValueError) as e:
            msg = f"[{job.get('name', '(unnamed)')}] {e}"
            errors.append(msg)
            print(f"::error::{msg}")
    return errors


def cmd_run(args) -> int:
    return 1 if run_all(args.config, args.dry_run) else 0


def cmd_playlists(args) -> int:
    sp = Spotify.from_env()
    for p in sp.my_playlists():
        if p:
            owner = (p.get("owner") or {}).get("display_name", "?")
            print(f"{p['id']}  {p['name']}  (owner: {owner})")
    return 0


def cmd_auth(args) -> int:
    cid, secret = env("SPOTIFY_CLIENT_ID"), env("SPOTIFY_CLIENT_SECRET")
    redirect = env("SPOTIFY_REDIRECT_URI", required=False) or DEFAULT_REDIRECT
    parsed = urllib.parse.urlparse(redirect)
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": redirect,
        "scope": SCOPES, "state": state,
    })
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path != parsed.path:
                self.send_response(404); self.end_headers(); return
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Done - you can close this tab and go back to the terminal.</h2>")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    print("Opening your browser to log in to Spotify. If it doesn't open, visit:\n\n" + url + "\n")
    webbrowser.open(url)
    while not result:
        server.handle_request()
    server.server_close()

    if result.get("state") != state:
        sys.exit("State mismatch - aborting.")
    if "error" in result:
        sys.exit(f"Spotify returned an error: {result['error']}")
    tokens = token_request({"grant_type": "authorization_code", "code": result["code"],
                            "redirect_uri": redirect}, cid, secret)
    print("Success. Save this as the SPOTIFY_REFRESH_TOKEN secret:\n")
    print(tokens["refresh_token"])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="log in once and print a refresh token").set_defaults(fn=cmd_auth)
    sub.add_parser("playlists", help="list your playlists and their IDs").set_defaults(fn=cmd_playlists)
    r = sub.add_parser("run", help="run all sync jobs")
    r.add_argument("--config", default="config.toml")
    r.add_argument("--dry-run", action="store_true", help="show what would be added without adding it")
    r.set_defaults(fn=cmd_run)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
