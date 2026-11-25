"""Run: python tests/test_auth.py (no dependencies; uses fakes, never calls Spotify or AWS).

The approval callback: when Spotify's daily quota is used up, /callback must show a clear
"try again later" page (not a 500) and must not touch the stored refresh token."""
import email.message
import io
import pathlib
import sys
import types
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeSSM:
    def __init__(self):
        self.store = {"/t/client-id": "cid", "/t/client-secret": "csec", "/t/user-id": "me",
                      "/t/link-key": "k" * 64, "/t/refresh-token": "old"}
        self.puts = []

    def get_parameters(self, Names, WithDecryption=False):
        return {"Parameters": [{"Name": n, "Value": self.store[n]} for n in Names if n in self.store]}

    def put_parameter(self, Name, Value, Type, Overwrite):
        self.puts.append(Name); self.store[Name] = Value


ssm = FakeSSM()
sys.modules["boto3"] = types.SimpleNamespace(client=lambda name: ssm if name == "ssm" else object())
import os
os.environ["PARAM_PREFIX"] = "/t"
from aws import auth_function as a

a.s.token_request = lambda form, cid, csec: {"access_token": "at", "refresh_token": "new"}
EVENT = {"rawPath": "/callback", "requestContext": {"domainName": "x.example"}}


def callback_event():
    return dict(EVENT, queryStringParameters={"code": "c", "state": a.make_state(a.link_key())})


def http_error(code, retry_after=None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://api.spotify.com/v1/me", code, "err", hdrs, io.BytesIO(b"{}"))


def urlopen_raising(err):
    def fake(req, timeout=None):
        raise err
    return fake


def test_quota_shows_retry_page_and_keeps_token():
    a.urllib.request.urlopen = urlopen_raising(http_error(429, 62_520))
    resp = a.handler(callback_event(), None)
    assert resp["statusCode"] == 503, resp
    assert "Spotify is busy" in resp["body"] and "UTC" in resp["body"]
    assert ssm.puts == [] and ssm.store["/t/refresh-token"] == "old"


def test_other_http_error_is_a_spotify_error_page():
    a.urllib.request.urlopen = urlopen_raising(http_error(500))
    resp = a.handler(callback_event(), None)
    assert resp["statusCode"] == 502 and "HTTP 500" in resp["body"], resp
    assert ssm.puts == []


def test_success_saves_token():
    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *e): return False
    a.urllib.request.urlopen = lambda req, timeout=None: Resp(b'{"id": "me"}')
    resp = a.handler(callback_event(), None)
    assert resp["statusCode"] == 200 and "All set" in resp["body"], resp
    assert ssm.store["/t/refresh-token"] == "new"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
