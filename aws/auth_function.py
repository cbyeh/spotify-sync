"""One-tap Spotify re-approval, hosted on a Lambda function URL.

Spotify refresh tokens expire 6 months after you approve the app. This function
lets you renew from any device where you're signed in to Spotify (e.g. your phone):

  GET  /start?k=<link key>   -> redirects to Spotify's approval page (auto-skips if already approved)
  GET  /callback             -> Spotify redirects here; saves the new refresh token to SSM
  {"action": "remind"}       -> (daily schedule) emails you the /start link once the token is ~5 months old

SSM parameters under PARAM_PREFIX (default /spotify-sync):
  client-id, client-secret         you create these
  link-key                         created by deploy.ps1; guards /start and signs `state`
  refresh-token, refresh-token-issued, user-id, last-reminded   written by this function

Safety: /start needs the secret link key, `state` is HMAC-signed and expires after
15 minutes, and after the first approval only the same Spotify user can replace the token.
"""
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import time
import urllib.parse
import urllib.request

import boto3

import spotify_sync as s

PREFIX = os.environ.get("PARAM_PREFIX", "/spotify-sync").rstrip("/")
BASE_URL = os.environ.get("AUTH_BASE_URL", "").rstrip("/") + "/"   # https://xxxx.lambda-url.<region>.on.aws/
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
REMIND_AFTER_DAYS = int(os.environ.get("REMIND_AFTER_DAYS", "150"))   # tokens die at ~182 days
REMIND_EVERY_DAYS = int(os.environ.get("REMIND_EVERY_DAYS", "7"))
STATE_TTL = 15 * 60

ssm = boto3.client("ssm")
sns = boto3.client("sns")


# --------------------------------------------------------------------------- SSM helpers

def get_params(*names: str) -> dict:
    full = [f"{PREFIX}/{n}" for n in names]
    resp = ssm.get_parameters(Names=full, WithDecryption=True)
    return {p["Name"].rsplit("/", 1)[1]: p["Value"] for p in resp["Parameters"]}


def put_param(name: str, value: str, secure: bool = False) -> None:
    ssm.put_parameter(Name=f"{PREFIX}/{name}", Value=value,
                      Type="SecureString" if secure else "String", Overwrite=True)


# --------------------------------------------------------------------------- helpers

def redirect_uri() -> str:
    return BASE_URL + "callback"


def sign(key: str, msg: str) -> str:
    return hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_state(key: str) -> str:
    ts = str(int(time.time()))
    return f"{ts}.{sign(key, ts)}"


def state_ok(key: str, state: str) -> bool:
    try:
        ts, sig = state.split(".", 1)
        fresh = 0 <= time.time() - int(ts) <= STATE_TTL
    except ValueError:
        return False
    return fresh and hmac.compare_digest(sig, sign(key, ts))


def page(status: int, title: str, body: str) -> dict:
    doc = (f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>{html.escape(title)}</title>"
           f"<body style='font-family:system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem'>"
           f"<h2>{html.escape(title)}</h2><p>{body}</p>")
    return {"statusCode": status, "headers": {"Content-Type": "text/html; charset=utf-8",
                                              "Cache-Control": "no-store"}, "body": doc}


def approval_link(key: str) -> str:
    return BASE_URL + "start?" + urllib.parse.urlencode({"k": key})


def spotify_user_id(access_token: str) -> str:
    req = urllib.request.Request(s.API + "/me", headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["id"]


# --------------------------------------------------------------------------- routes

def start(qs: dict) -> dict:
    p = get_params("client-id", "link-key")
    if not hmac.compare_digest(qs.get("k", ""), p.get("link-key", "\0")):
        return page(403, "Link not valid", "Use the approval link from your email or from deploy.ps1.")
    url = s.AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": p["client-id"], "response_type": "code", "redirect_uri": redirect_uri(),
        "scope": s.SCOPES, "state": make_state(p["link-key"]),
    })
    return {"statusCode": 302, "headers": {"Location": url, "Cache-Control": "no-store"}}


def callback(qs: dict) -> dict:
    p = get_params("client-id", "client-secret", "link-key", "user-id")
    if not state_ok(p.get("link-key", ""), qs.get("state", "")):
        return page(400, "Link expired", "Open your approval link again and finish within 15 minutes.")
    if "error" in qs:
        return page(400, "Spotify didn't approve", html.escape(qs["error"]))

    tokens = s.token_request({"grant_type": "authorization_code", "code": qs.get("code", ""),
                              "redirect_uri": redirect_uri()}, p["client-id"], p["client-secret"])
    uid = spotify_user_id(tokens["access_token"])
    if p.get("user-id") and uid != p["user-id"]:
        return page(403, "Wrong Spotify account",
                    "You approved with a different Spotify account than this sync uses. "
                    "Sign in to the right account and try again.")

    now = dt.datetime.now(dt.timezone.utc)
    put_param("refresh-token", tokens["refresh_token"], secure=True)
    put_param("refresh-token-issued", now.isoformat(timespec="seconds"))
    put_param("user-id", uid)
    due_dt = now + dt.timedelta(days=REMIND_AFTER_DAYS)
    due = f"{due_dt:%B} {due_dt.day}, {due_dt.year}"
    return page(200, "All set ✅",
                f"Spotify access renewed for <b>{html.escape(uid)}</b>. "
                f"You'll get an email around {due} when it's time to tap the link again.")


def remind() -> dict:
    p = get_params("refresh-token-issued", "last-reminded", "link-key")
    now = dt.datetime.now(dt.timezone.utc)
    if "refresh-token-issued" not in p:
        print("No approval recorded yet; nothing to remind about.")
        return {"reminded": False}
    issued = dt.datetime.fromisoformat(p["refresh-token-issued"])
    age = (now - issued).days
    if age < REMIND_AFTER_DAYS:
        print(f"Token is {age} days old; reminder starts at {REMIND_AFTER_DAYS}.")
        return {"reminded": False}
    if "last-reminded" in p and (now - dt.datetime.fromisoformat(p["last-reminded"])).days < REMIND_EVERY_DAYS:
        return {"reminded": False}
    if not TOPIC_ARN:
        print("::warning::Token needs renewal but no TOPIC_ARN is set; re-deploy with -AlertEmail.")
        return {"reminded": False}

    expires = issued + dt.timedelta(days=182)
    status = ("has EXPIRED - syncing is paused until you renew." if now >= expires
              else f"expires around {expires:%B %d}.")
    sns.publish(TopicArn=TOPIC_ARN, Subject="spotify-sync: tap to renew Spotify access",
                Message=(f"Your Spotify access for spotify-sync {status}\n\n"
                         f"Open this link on any device where you're signed in to Spotify:\n\n"
                         f"{approval_link(p['link-key'])}\n\n"
                         f"It takes one tap. You'll be reminded weekly until it's renewed."))
    put_param("last-reminded", now.isoformat(timespec="seconds"))
    print(f"Sent renewal reminder (token age {age} days).")
    return {"reminded": True}


def handler(event, context):
    event = event or {}
    if event.get("action") == "remind":
        return remind()
    path = event.get("rawPath", "/")
    qs = event.get("queryStringParameters") or {}
    try:
        if path == "/start":
            return start(qs)
        if path == "/callback":
            return callback(qs)
    except s.SpotifyError as e:
        print(f"::error::{e}")
        return page(502, "Spotify error", html.escape(str(e)))
    return page(404, "Not found", "")
