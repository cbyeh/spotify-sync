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
import datetime as dt
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
FULL_REFRESH_SECONDS = 7 * 24 * 3600   # re-read a cached playlist fully at least weekly
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
    "user-read-playback-state",      # via = "playback" jobs: see devices, queue, what's playing
    "user-modify-playback-state",    # via = "playback" jobs: briefly play a playlist (muted), then restore
])

MADE_FOR_YOU_HINT = (
    "Spotify only returns the tracks of playlists you own or collaborate on, and since "
    "Nov 27 2024 personal (Development Mode) apps cannot read Spotify-generated playlists "
    "such as Discover Weekly, Release Radar or Daily Mix at all."
)


class SpotifyError(Exception):
    pass


class QuotaExceeded(SpotifyError):
    """Spotify's quota for this developer account is used up; retry_after is in seconds."""

    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


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
        # playlist id -> (snapshot_id, track uris, time of last full read). Survives between runs while
        # the session is reused; with a `store` (e.g. S3) it also survives cold starts, so a big
        # destination is only re-read when something outside this tool changed it.
        self._playlists: dict[str, tuple[str, list[str], float]] = {}
        self.store = None   # optional: object with get(pid) -> dict | None, put(pid, dict), delete(pid)

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
                try:
                    return json.loads(raw) if raw.strip() else {}
                except ValueError:
                    return {}   # some player commands answer with a non-JSON body
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    wait = int(e.headers.get("Retry-After", 2 ** attempt) or 1)
                    if e.code == 429 and wait > 120:
                        # A quota reset hours away, not a brief rate limit: retrying now only burns time.
                        raise QuotaExceeded(f"{method} {url} -> 429 quota exceeded; resets in "
                                            f"{wait // 3600}h{wait % 3600 // 60:02d}m", wait) from None
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

    def _cached(self, playlist_id: str) -> tuple | None:
        cache = self._playlists
        if playlist_id not in cache and getattr(self, "store", None) is not None:
            try:
                entry = self.store.get(playlist_id)
            except Exception as e:   # the cache is an optimization; never fail a run over it
                print(f"::warning::playlist cache read failed for {playlist_id}: {e}")
                entry = None
            if entry and entry.get("snapshot"):
                cache[playlist_id] = (entry["snapshot"], list(entry.get("uris", [])),
                                      float(entry.get("full_read_at", 0)))
        return cache.get(playlist_id)

    def playlist_uris(self, playlist_id: str) -> list[str]:
        """Track URIs of a playlist we own. Uses the cached copy while Spotify's snapshot_id is
        unchanged (re-reading every page only when the playlist was modified elsewhere, or at
        least once every FULL_REFRESH_SECONDS as a safety net)."""
        cache = getattr(self, "_playlists", None)
        if cache is None:   # minimal clients (tests) without caching
            return [u for page in self.pages(f"/playlists/{playlist_id}/items?limit=100&additional_types=track")
                    for u in map(track_uri, page) if u]
        snapshot = self.playlist_snapshot(playlist_id)
        hit = self._cached(playlist_id)
        if snapshot and hit and hit[0] == snapshot and time.time() - hit[2] < FULL_REFRESH_SECONDS:
            return list(hit[1])
        uris = []
        for page in self.pages(f"/playlists/{playlist_id}/items?limit=100&additional_types=track"):
            uris += [u for u in map(track_uri, page) if u]
        self._remember(playlist_id, snapshot, uris, full_read=True)
        return list(uris)

    def _remember(self, playlist_id: str, snapshot: str | None, uris: list[str],
                  full_read: bool = False) -> None:
        cache = getattr(self, "_playlists", None)
        if cache is None:
            return
        store = getattr(self, "store", None)
        try:
            if not snapshot:
                cache.pop(playlist_id, None)
                if store is not None:
                    store.delete(playlist_id)
                return
            read_at = time.time() if full_read else cache.get(playlist_id, (None, None, 0.0))[2]
            cache[playlist_id] = (snapshot, list(uris), read_at)
            if store is not None:
                store.put(playlist_id, {"snapshot": snapshot, "uris": list(uris), "full_read_at": read_at})
        except Exception as e:
            print(f"::warning::playlist cache write failed for {playlist_id}: {e}")

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
        known = cache.get(playlist_id, (None, [], 0.0))[1]
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


# --------------------------------------------------------------------------- read a playlist by playing it

class PlaybackUnavailable(Exception):
    """Can't use playback right now (music is playing, or no suitable Spotify app is open)."""


def _track_obj_uri(obj: dict | None) -> str | None:
    if not obj or obj.get("type") != "track" or obj.get("is_local"):
        return None
    return obj.get("uri")


def _wait_until(sp: "Spotify", ok, timeout: float = 8.0) -> dict | None:
    """Poll what's playing until ok(state) is true; None on timeout."""
    end = time.time() + timeout
    while True:
        cur = sp.request("GET", "/me/player/currently-playing") or {}
        if ok(cur):
            return cur
        if time.time() >= end:
            return None
        SLEEP(0.5)


SLEEP = time.sleep   # replaced in tests


def read_by_playback(sp: "Spotify", playlist_id: str, device_name: str | None = None,
                     device_types: tuple = ("Computer",), max_tracks: int = 60,
                     trace: bool = False, take: int | None = None) -> list[str]:
    """Read a playlist the API won't let us read (e.g. Discover Weekly) by briefly playing it,
    muted, on an idle Spotify app, then putting everything back the way it was.

    Plays at most a handful of positions (each for ~1 second, well under the 30 s a stream needs):
    the first track plus the queue gives ~21 tracks; jumping to later positions verifies each
    candidate really belongs to the playlist, which also finds where the playlist ends and
    autoplay begins. Raises PlaybackUnavailable if something is playing, no device fits, the
    player state can't be read (nothing is changed then), or it reads more than `max_tracks`
    (a safety net: nothing is added). `trace=True` logs every position it plays.

    `take`: read exactly this many positions (e.g. 30 for Discover Weekly). Spotify keeps serving
    tracks past the end of some generated playlists while still reporting the playlist as the
    source, so "play until it stops" can't find their end; a known length avoids that."""
    if take:
        max_tracks = take + 1
    ctx = f"spotify:playlist:{playlist_id}"
    before = sp.request("GET", "/me/player") or {}
    if before.get("is_playing"):
        # Spotify sometimes reports "playing" for a client that stopped syncing (position frozen).
        # Either way, don't act: real playback mustn't be interrupted, and stale state can't be
        # trusted for restoring settings.
        SLEEP(2.5)
        again = sp.request("GET", "/me/player") or {}
        if again.get("is_playing") and again.get("progress_ms") == before.get("progress_ms"):
            raise PlaybackUnavailable("Spotify reports playback but the position isn't moving: the player's "
                                      "state looks out of sync (press play/pause in the app to resync)")
        raise PlaybackUnavailable("something is playing right now")
    devices = sp.request("GET", "/me/player/devices").get("devices", [])
    device = next((d for d in devices if not d.get("is_restricted") and (
        d.get("name") == device_name if device_name else d.get("type") in device_types)), None)
    if not device:
        wanted = f'"{device_name}"' if device_name else "a computer"
        raise PlaybackUnavailable(f"no idle Spotify app open on {wanted}")
    did = device["id"]
    q = f"device_id={did}"
    print(f"playback: using {device.get('name')} ({device.get('type')})")

    def log(msg: str) -> None:
        if trace:
            print(f"trace: {msg}")

    def describe(c: dict) -> str:
        item = c.get("item") or {}
        ctx_uri = (c.get("context") or {}).get("uri") or "-"
        where = "this playlist" if ctx_uri == ctx else ctx_uri
        return f"{'playing' if c.get('is_playing') else 'paused'} '{item.get('name', '-')}' from {where}"

    # Record the state to restore. If a session is active, that's what you'd come back to;
    # otherwise activate the idle device (without playing) and wait until it reports its state.
    if before.get("device"):
        state = before
        sp.request("PUT", "/me/player", {"device_ids": [did], "play": False})
    else:
        sp.request("PUT", "/me/player", {"device_ids": [did], "play": False})
        state = {}
        end = time.time() + 10
        while time.time() < end:
            state = sp.request("GET", "/me/player") or {}
            if (state.get("device") or {}).get("id") == did:
                break
            SLEEP(0.5)
        if (state.get("device") or {}).get("id") != did:
            raise PlaybackUnavailable("couldn't read the player's settings, so nothing was changed")
    if state.get("is_playing"):
        raise PlaybackUnavailable("something started playing")
    log(f"before: {describe(state)}, shuffle={state.get('shuffle_state')}, repeat={state.get('repeat_state')}, "
        f"volume={(state.get('device') or {}).get('volume_percent')}")
    prev = {
        "device": (before.get("device") or {}).get("id"),
        "shuffle": state.get("shuffle_state"),
        "repeat": state.get("repeat_state"),
        # the device we mute is `did`: save its own volume (not the previously active device's)
        "volume": ((state.get("device") or {}).get("volume_percent")
                   if (state.get("device") or {}).get("id") == did else device.get("volume_percent")),
        "context": (state.get("context") or {}).get("uri"),
        "item": _track_obj_uri(state.get("item")),
        "progress": state.get("progress_ms") or 0,
    }
    can_mute = device.get("supports_volume", True) and prev["volume"] is not None

    def play_at(pos: int) -> None:
        sp.request("PUT", f"/me/player/play?{q}", {"context_uri": ctx, "offset": {"position": pos}})

    def playing(c: dict) -> bool:
        return bool(c.get("is_playing")) and (c.get("context") or {}).get("uri") == ctx

    def check(pos: int, expected: str) -> bool:
        try:
            play_at(pos)
        except SpotifyError as e:
            log(f"check {pos}: play refused ({str(e)[-80:]})")
            return False
        ok = _wait_until(sp, lambda c: playing(c) and _track_obj_uri(c.get("item")) == expected, timeout=8)
        if trace:
            now = sp.request("GET", "/me/player/currently-playing") or {}
            log(f"check {pos}: {'OK ' if ok else 'NO '} now {describe(now)}")
        return ok is not None

    try:
        if can_mute:
            # If muting fails, this raises and nothing gets played out loud.
            sp.request("PUT", f"/me/player/volume?volume_percent=0&{q}")
        sp.request("PUT", f"/me/player/shuffle?state=false&{q}")
        sp.request("PUT", f"/me/player/repeat?state=off&{q}")

        play_at(0)
        cur = _wait_until(sp, playing, timeout=20)   # a receiver waking from standby can be slow
        if not cur:
            raise PlaybackUnavailable("Spotify didn't start the playlist")
        log(f"pos 0: {describe(cur)}")
        tracks = [_track_obj_uri(cur.get("item"))]   # confirmed members, in playlist order
        one_by_one = False
        limit = take or max_tracks
        while len(tracks) < limit:
            if not one_by_one:
                # The queue shows what comes next: the rest of the playlist, then possibly autoplay.
                queue = sp.request("GET", "/me/player/queue").get("queue", [])
                log(f"queue after {len(tracks)}: {len(queue)} items: " +
                    ", ".join(q.get("name", "?")[:20] for q in queue[:20]))
                candidates = (tracks + [u for u in map(_track_obj_uri, queue) if u])[:limit]
                lo, hi = len(tracks) - 1, len(candidates) - 1
                if hi > lo and check(hi, candidates[hi]):
                    lo = hi                       # common case: the whole queue is playlist
                else:
                    while lo < hi - 1:            # find where the playlist ends inside the queue
                        mid = (lo + hi) // 2
                        if check(mid, candidates[mid]):
                            lo = mid
                        else:
                            hi = mid
                if lo == len(tracks) - 1 and len(candidates) > len(tracks):
                    one_by_one = True             # queue doesn't line up (e.g. songs you queued by hand)
                tracks = candidates[:lo + 1]
            if len(tracks) >= limit:
                break
            # Start at the next position: either finds one more track or confirms the end.
            try:
                play_at(len(tracks))
            except SpotifyError as e:
                log(f"probe {len(tracks)}: play refused ({str(e)[-80:]}) -> end")
                break
            nxt = _wait_until(sp, lambda c: playing(c) and _track_obj_uri(c.get("item")) not in tracks,
                              timeout=6)
            if not nxt:
                now = sp.request("GET", "/me/player/currently-playing") or {}
                log(f"probe {len(tracks)}: nothing new, now {describe(now)} -> end")
                break
            log(f"probe {len(tracks)}: {describe(nxt)}")
            tracks.append(_track_obj_uri(nxt.get("item")))
        result = [u for u in tracks if u][:limit]
        print(f"playback: read {len(result)} tracks")
        if take and len(result) < take:
            print(f"::warning::expected {take} tracks but the playlist ended after {len(result)}")
        if not take and len(result) >= max_tracks:
            raise PlaybackUnavailable(f"read {len(result)}+ tracks, more than the {max_tracks} expected; "
                                      "not trusting this read, nothing added")
    finally:
        _restore(sp, did, prev, can_mute)
    return list(dict.fromkeys(result))


def _restore(sp: "Spotify", did: str, prev: dict, can_mute: bool) -> None:
    """Put playback back the way it was; best effort, never raises.

    Spotify applies player commands asynchronously (a pause can land before a play, and loading
    a context can reset shuffle/repeat), so after issuing the commands we re-read the player and
    keep correcting until it matches. The volume is only restored once the player is confirmed
    paused: if that can't be confirmed, the device stays muted rather than playing out loud."""
    q = f"device_id={did}"

    def attempt(method, path, body=None):
        try:
            sp.request(method, path, body)
        except SpotifyError as e:
            if "/pause" in path and "Restriction violated" in str(e):
                return   # already paused
            print(f"::warning::restore step failed: {method} {path}: {e}")

    def current() -> dict:
        try:
            return sp.request("GET", "/me/player") or {}
        except SpotifyError:
            return {}

    print("playback: restoring player")
    attempt("PUT", f"/me/player/pause?{q}")
    if prev["item"]:
        body = ({"context_uri": prev["context"], "offset": {"uri": prev["item"]}} if prev["context"]
                else {"uris": [prev["item"]]})
        body["position_ms"] = prev["progress"]
        attempt("PUT", f"/me/player/play?{q}", body)
        # let the reload land before touching anything else
        _wait_until(sp, lambda c: c.get("is_playing") and _track_obj_uri(c.get("item")) == prev["item"], timeout=8)

    paused = False
    for _ in range(8):
        attempt("PUT", f"/me/player/pause?{q}")
        if prev["shuffle"] is not None:
            attempt("PUT", f"/me/player/shuffle?state={'true' if prev['shuffle'] else 'false'}&{q}")
        if prev["repeat"]:
            attempt("PUT", f"/me/player/repeat?state={prev['repeat']}&{q}")
        SLEEP(1.5)
        cur = current()
        paused = cur.get("is_playing") is False
        ok = (paused
              and (prev["shuffle"] is None or cur.get("shuffle_state") == prev["shuffle"])
              and (not prev["repeat"] or cur.get("repeat_state") == prev["repeat"])
              and (not prev["item"] or _track_obj_uri(cur.get("item")) == prev["item"]))
        if ok:
            break
    else:
        print("::warning::player didn't fully match its previous state after restoring")

    if can_mute:
        if paused:
            attempt("PUT", f"/me/player/volume?volume_percent={prev['volume']}&{q}")
        else:
            print("::warning::couldn't confirm the player is paused, so it was left muted")
    after = current()
    print(f"playback: after restore: shuffle={after.get('shuffle_state')}, repeat={after.get('repeat_state')}, "
          f"volume={(after.get('device') or {}).get('volume_percent')}, "
          f"item='{(after.get('item') or {}).get('name')}', playing={after.get('is_playing')}")
    if prev["device"] and prev["device"] != did:
        attempt("PUT", "/me/player", {"device_ids": [prev["device"]], "play": False})


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


def is_playback_job(job: dict) -> bool:
    via = job.get("via")
    if via not in (None, "playback"):
        raise ValueError('`via` must be "playback" (or left out)')
    return via == "playback"


def run_playback_job(sp: Spotify, job: dict, dry_run: bool, state=None,
                     final: bool = False, notify=None) -> int:
    """Copy a playlist the API can't read (e.g. Discover Weekly) by briefly playing it.
    Runs at most once per week. If it can't run (device unavailable, something playing) or finds
    nothing new yet, a later attempt retries; the `final` attempt emails you if it never got to run."""
    name = job.get("name", "(unnamed)")
    src_id, dest_id = parse_playlist_id(job["from"]), parse_playlist_id(job["to"])
    year, week, _ = dt.datetime.now(dt.timezone.utc).isocalendar()
    key, tag = f"playback-{src_id}-{dest_id}", f"{year}-W{week:02d}"
    if state is not None and state.get(key) == tag:
        print(f"[{name}] already done this week.")
        return 0
    try:
        take = job.get("take", 30)
        uris = read_by_playback(sp, src_id, device_name=job.get("device"),
                                max_tracks=int(job.get("max_tracks", 60)), trace=bool(job.get("trace") or dry_run),
                                take=int(take) if take else None)
        if not uris:
            raise PlaybackUnavailable("couldn't read any tracks")
    except PlaybackUnavailable as e:
        print(f"[{name}] not this time: {e}.")
        if final and notify:
            notify(f"spotify-sync: couldn't copy {name} this week",
                   f"{name}: {e} at every attempt this week.\n\n"
                   "Open the playlist in Spotify, select all (Ctrl/Cmd+A) and add the songs to your archive. "
                   "The sync skips any that are already there.\n\n"
                   "To let it run automatically, leave the Spotify desktop app open (and the computer awake) "
                   "on Monday mornings.")
        return 0
    existing = set(sp.playlist_uris(dest_id))
    new = [u for u in uris if u not in existing]
    verb = "Would add" if dry_run else "Adding"
    print(f"[{name}] read {len(uris)} tracks by playback; {verb} {len(new)} new.")
    if new and not dry_run:
        sp.add_to_playlist(dest_id, new)
    if state is not None and not dry_run:
        if new or final:
            state.set(key, tag)
        else:
            print(f"[{name}] nothing new yet (playlist may not have refreshed); will retry.")
    return len(new)


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
            state=None, full: bool = False, sp: Spotify | None = None,
            playback: bool = False, final: bool = False, notify=None) -> list[str]:
    """Run every job; returns a list of error messages (empty means all succeeded).
    Pass a `state` store to enable quick "anything new?" checks, and `sp` to reuse a session.
    `playback=True` runs only the via = "playback" jobs (they have their own weekly schedule)."""
    jobs = [j for j in load_config(config_path) if is_playback_job(j) == playback]
    if not jobs:
        return []
    if sp is None or sp.expired:
        sp = Spotify.from_env(on_new_refresh_token)
    sp.new_run()
    errors = []
    for job in jobs:
        try:
            if playback:
                run_playback_job(sp, job, dry_run, state, final, notify)
            else:
                run_job(sp, job, dry_run, state, full)
        except QuotaExceeded:
            raise   # the whole run should stop and back off, not just this job
        except (SpotifyError, ValueError) as e:
            msg = f"[{job.get('name', '(unnamed)')}] {e}"
            errors.append(msg)
            print(f"::error::{msg}")
    return errors


def cmd_run(args) -> int:
    return 1 if run_all(args.config, args.dry_run, playback=args.playback) else 0


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
    r.add_argument("--playback", action="store_true", help='run only the via = "playback" jobs')
    r.set_defaults(fn=cmd_run)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
