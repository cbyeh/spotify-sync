"""AWS Lambda entry point for spotify-sync.

Secrets come from SSM Parameter Store (SecureString, free tier) under PARAM_PREFIX:
  <prefix>/client-id, <prefix>/client-secret, <prefix>/refresh-token
If Spotify rotates the refresh token, the new one is written back automatically.

Runs every minute. To stay cheap, each job first asks "anything new?" (newest like / newest
play / playlist snapshot) and compares it with what it saw last time, stored under
<prefix>/state/. Only when something changed does it read and write playlists.
Invoke with {"full": true} to ignore that and re-check everything (a daily schedule does this),
or {"dry_run": true} to see what would change. {"playback": true} runs only the via = "playback"
jobs (weekly schedule); add "final": true on the last attempt so you get an email if it never ran.
"""
import datetime as dt
import os

import boto3

import spotify_sync

PREFIX = os.environ.get("PARAM_PREFIX", "/spotify-sync").rstrip("/")
LOCK = f"{PREFIX}/lock"
LOCK_STALE_SECONDS = 360   # longer than the 5-minute function timeout
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
ssm = boto3.client("ssm")

_session: spotify_sync.Spotify | None = None   # reused across warm invocations until the token expires


def _load_secrets() -> bool:
    names = {
        f"{PREFIX}/client-id": "SPOTIFY_CLIENT_ID",
        f"{PREFIX}/client-secret": "SPOTIFY_CLIENT_SECRET",
        f"{PREFIX}/refresh-token": "SPOTIFY_REFRESH_TOKEN",
    }
    resp = ssm.get_parameters(Names=list(names), WithDecryption=True)
    missing = resp["InvalidParameters"]
    if missing == [f"{PREFIX}/refresh-token"]:
        return False   # not approved yet
    if missing:
        raise RuntimeError(f"Missing SSM parameters: {missing}")
    for p in resp["Parameters"]:
        os.environ[names[p["Name"]]] = p["Value"]
    return True


def _save_refresh_token(token: str) -> None:
    ssm.put_parameter(Name=f"{PREFIX}/refresh-token", Value=token,
                      Type="SecureString", Overwrite=True)


class SsmState:
    """Last-seen change markers, one String parameter per job under <prefix>/state/."""

    def __init__(self):
        path = f"{PREFIX}/state/"
        resp = ssm.get_parameters_by_path(Path=path)
        self.values = {p["Name"][len(path):]: p["Value"] for p in resp.get("Parameters", [])}
        self.path = path

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        if self.values.get(key) != value:
            ssm.put_parameter(Name=self.path + key, Value=value, Type="String", Overwrite=True)
            self.values[key] = value


def _acquire_lock() -> bool:
    """Stop two overlapping runs from adding the same tracks twice."""
    now = dt.datetime.now(dt.timezone.utc)
    try:
        ssm.put_parameter(Name=LOCK, Value=now.isoformat(), Type="String", Overwrite=False)
        return True
    except ssm.exceptions.ParameterAlreadyExists:
        held_since = dt.datetime.fromisoformat(ssm.get_parameter(Name=LOCK)["Parameter"]["Value"])
        if (now - held_since).total_seconds() < LOCK_STALE_SECONDS:
            return False
        ssm.put_parameter(Name=LOCK, Value=now.isoformat(), Type="String", Overwrite=True)  # stale: take over
        return True


def _release_lock() -> None:
    try:
        ssm.delete_parameter(Name=LOCK)
    except ssm.exceptions.ParameterNotFound:
        pass


def _notify(subject: str, message: str) -> None:
    if TOPIC_ARN:
        boto3.client("sns").publish(TopicArn=TOPIC_ARN, Subject=subject[:100], Message=message)
    else:
        print(f"::warning::{subject} (no TOPIC_ARN set, so no email)")


def _config_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    # a flat zip has config.toml next to this file; the CDK asset keeps the repo layout (one level up)
    return next(p for p in (os.path.join(here, "config.toml"), os.path.join(here, "..", "config.toml"))
                if os.path.exists(p))


def handler(event, context):
    global _session
    event = event or {}
    dry_run = bool(event.get("dry_run", False))
    full = bool(event.get("full", False))
    playback = bool(event.get("playback", False))
    final = bool(event.get("final", False))

    if _session is None or _session.expired:
        if not _load_secrets():
            print("Waiting for first Spotify approval; skipping.")
            return {"ok": True, "skipped": "not approved yet"}
        _session = spotify_sync.Spotify.from_env(on_new_refresh_token=_save_refresh_token)

    if not _acquire_lock():
        print("Previous run still in progress; skipping.")
        return {"ok": True, "skipped": "locked"}
    try:
        errors = spotify_sync.run_all(_config_path(), dry_run, state=SsmState(), full=full, sp=_session,
                                      playback=playback, final=final, notify=_notify)
    finally:
        _release_lock()
    if errors:
        _session = None   # start fresh next time (e.g. if the token was revoked)
        # Raising marks the invocation as failed, which feeds the error alarm.
        raise RuntimeError("; ".join(errors))
    return {"ok": True, "full": full, "playback": playback}
