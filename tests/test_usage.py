"""Run: python tests/test_usage.py (no dependencies; uses fakes, never calls Spotify or AWS).

Spotify request counting: each run logs its requests and adds them to a 24-hour window total in
SSM, the window starts over after 24 h, a quota hit records the total it was reached at, and a
failure to record usage never breaks the sync."""
import contextlib
import datetime as dt
import io
import json
import pathlib
import sys
import types
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class NotFound(Exception): pass
class Exists(Exception): pass


class FakeSSM:
    exceptions = types.SimpleNamespace(ParameterNotFound=NotFound, ParameterAlreadyExists=Exists)

    def __init__(self):
        self.store = {}
        self.fail_usage = False

    def get_parameter(self, Name):
        if Name not in self.store:
            raise NotFound(Name)
        return {"Parameter": {"Value": self.store[Name]}}

    def put_parameter(self, Name, Value, Type, Overwrite):
        if Name.endswith("api-usage") and self.fail_usage:
            raise RuntimeError("SSM down")
        if not Overwrite and Name in self.store:
            raise Exists(Name)
        self.store[Name] = Value

    def delete_parameter(self, Name):
        self.store.pop(Name, None)

    def get_parameters_by_path(self, **kw):
        return {"Parameters": []}


ssm = FakeSSM()
sys.modules["boto3"] = types.SimpleNamespace(client=lambda name: ssm)
from aws import lambda_function as lf
import spotify_sync as s

now = [dt.datetime(2026, 11, 25, 21, 0, tzinfo=dt.timezone.utc)]


class Clock(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return now[0]


lf.dt = types.SimpleNamespace(datetime=Clock, timezone=dt.timezone, timedelta=dt.timedelta)


class FakeSession:
    expired = False
    store = None
    calls = Counter()


lf._session = FakeSession()
lf.CACHE_BUCKET = ""
plan = {"calls": {}, "raise": None}


def fake_run_all(*a, sp, **kw):
    sp.calls.update(plan["calls"])
    if plan["raise"]:
        raise plan["raise"]
    return []


s.run_all = fake_run_all


def invoke(calls, exc=None):
    plan["calls"], plan["raise"] = calls, exc
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = lf.handler({}, None)
    return result, out.getvalue()


def usage():
    return json.loads(ssm.store[lf.USAGE])


def test_counts_accumulate_and_roll_over():
    _, log = invoke({"GET /me/tracks": 1, "GET /me/player/recently-played": 1})
    assert "2 this run, 2 in the window" in log, log
    now[0] += dt.timedelta(minutes=10)
    invoke({"GET /me/tracks": 1, "GET /me/player/recently-played": 1})
    assert usage()["calls"] == 4 and usage()["by"]["GET /me/tracks"] == 2
    now[0] += dt.timedelta(hours=24)
    _, log = invoke({"GET /me/tracks": 1})
    assert "window from 2026-11-25T21:00 UTC: 4" in log, log
    assert usage()["calls"] == 1 and usage()["start"].startswith(now[0].isoformat()[:16])


def test_quota_hit_records_total_and_pauses():
    before = usage()["calls"]
    result, log = invoke({"GET /me/tracks": 3}, s.QuotaExceeded("GET /me/tracks -> 429", 3600))
    assert result["skipped"] == "quota"
    assert usage()["calls"] == before + 3
    assert "quota exhausted at this total" in log, log
    ssm.store.pop(lf.BACKOFF)


def test_usage_failure_never_breaks_the_run():
    ssm.fail_usage = True
    result, log = invoke({"GET /me/tracks": 1})
    ssm.fail_usage = False
    assert result["ok"] and "Couldn't record Spotify usage" in log, log


def test_request_counts_by_endpoint():
    assert s.endpoint_name("GET", "https://api.spotify.com/v1/playlists/abc/items?limit=100&offset=200") \
        == "GET /playlists/{id}/items"
    assert s.endpoint_name("GET", "https://api.spotify.com/v1/me/tracks?limit=1") == "GET /me/tracks"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
