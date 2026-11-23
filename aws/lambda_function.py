"""AWS Lambda entry point for spotify-sync.

Secrets come from SSM Parameter Store (SecureString, free tier) under PARAM_PREFIX:
  <prefix>/client-id, <prefix>/client-secret, <prefix>/refresh-token
If Spotify rotates the refresh token, the new one is written back automatically.
"""
import os

import boto3

import spotify_sync

PREFIX = os.environ.get("PARAM_PREFIX", "/spotify-sync").rstrip("/")
ssm = boto3.client("ssm")


def _load_secrets() -> bool:
    names = {
        f"{PREFIX}/client-id": "SPOTIFY_CLIENT_ID",
        f"{PREFIX}/client-secret": "SPOTIFY_CLIENT_SECRET",
        f"{PREFIX}/refresh-token": "SPOTIFY_REFRESH_TOKEN",
    }
    resp = ssm.get_parameters(Names=list(names), WithDecryption=True)
    missing = resp["InvalidParameters"]
    if missing == [f"{PREFIX}/refresh-token"]:
        return False   # not approved yet: open the approval link printed by deploy.ps1
    if missing:
        raise RuntimeError(f"Missing SSM parameters: {missing}")
    for p in resp["Parameters"]:
        os.environ[names[p["Name"]]] = p["Value"]
    return True


def _save_refresh_token(token: str) -> None:
    ssm.put_parameter(Name=f"{PREFIX}/refresh-token", Value=token,
                      Type="SecureString", Overwrite=True)


def handler(event, context):
    if not _load_secrets():
        print("Waiting for first Spotify approval; open the approval link from deploy.ps1. Skipping.")
        return {"ok": True, "skipped": "not approved yet"}
    dry_run = bool((event or {}).get("dry_run", False))
    config = os.path.join(os.path.dirname(__file__), "config.toml")
    errors = spotify_sync.run_all(config, dry_run, on_new_refresh_token=_save_refresh_token)
    if errors:
        # Raising marks the invocation as failed, which triggers the error alarm.
        raise RuntimeError("; ".join(errors))
    return {"ok": True}
