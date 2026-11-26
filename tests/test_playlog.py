"""Run: python tests/test_playlog.py (no dependencies; uses fakes, never calls Spotify or AWS).

Listening-history log: each finished play is written once to the history bucket, oldest first,
using the plays the run already fetched; a possible gap (more than 50 plays since the last run) is
flagged; dry runs and runs that didn't read recent plays write nothing; and an S3 failure never
breaks the sync and doesn't advance the marker past unwritten plays."""
import contextlib
import io
import json
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class NotFound(Exception): pass
class Exists(Exception): pass


class FakeSSM:
    exceptions = types.SimpleNamespace(ParameterNotFound=NotFound, ParameterAlreadyExists=Exists)

    def __init__(self): self.store = {}
    def get_parameter(self, Name):
        if Name not in self.store:
            raise NotFound(Name)
        return {"Parameter": {"Value": self.store[Name]}}
    def put_parameter(self, Name, Value, Type, Overwrite): self.store[Name] = Value


class FakeS3:
    def __init__(self, fail_after=None): self.objects = {}; self.fail_after = fail_after
    def put_object(self, Bucket, Key, Body, ContentType):
        if self.fail_after is not None and len(self.objects) >= self.fail_after:
            raise RuntimeError("S3 down")
        self.objects[Key] = json.loads(Body)


ssm = FakeSSM()
sys.modules["boto3"] = types.SimpleNamespace(client=lambda name: ssm)
from aws import lambda_function as lf  # noqa: E402
import spotify_sync as s  # noqa: E402

lf.HISTORY_BUCKET = "history"


def play(minute, n=1, ctx="spotify:playlist:abc", kind="track"):
    return {"played_at": f"2026-11-25T10:{minute:02d}:00.000Z",
            "context": {"type": "playlist", "uri": ctx} if ctx else None,
            "track": {"type": kind, "uri": f"spotify:track:{n}", "name": f"Song {n}", "duration_ms": 200000,
                      "explicit": False, "artists": [{"name": "Artist", "uri": "spotify:artist:a"}],
                      "album": {"name": "Album", "uri": "spotify:album:x", "release_date": "2020"}}}


class Sp:
    def __init__(self, items): self._recent = items


def run(items, s3):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        n = lf._log_plays(Sp(items), s3=s3)
    return n, out.getvalue()


# first run: everything Spotify returned, oldest first; the marker is the newest play
s3 = FakeS3()
n, _ = run([play(5, 3), play(3, 2), play(1, 1)], s3)
assert n == 3
keys = sorted(s3.objects)
assert keys[0] == "plays/2026/11/25/2026-11-25T10-01-00.000Z.json", keys
rec = s3.objects[keys[0]]
assert rec["uri"] == "spotify:track:1" and rec["artists"][0]["name"] == "Artist" and rec["context"]["uri"] == "spotify:playlist:abc"
assert ssm.store[lf.PLAY_LOG] == "2026-11-25T10:05:00.000Z"

# next run: only the new play; nothing is written twice
n, _ = run([play(9, 4), play(5, 3), play(3, 2)], s3)
assert n == 1 and len(s3.objects) == 4
assert ssm.store[lf.PLAY_LOG] == "2026-11-25T10:09:00.000Z"

# nothing new: no writes
n, _ = run([play(9, 4)], s3)
assert n == 0 and len(s3.objects) == 4

# episodes and items without a track are skipped; no context is fine
n, _ = run([play(12, 6, kind="episode"), play(11, 5, ctx=None)], s3)
assert n == 1 and s3.objects["plays/2026/11/25/2026-11-25T10-11-00.000Z.json"]["context"] is None

# a run that didn't read recent plays writes nothing and makes no AWS calls
assert lf._log_plays(Sp(None), s3=None) == 0

# 50 fresh plays, all newer than the marker: flagged as a possible gap, still logged
many = [{**play(0, i), "played_at": f"2026-11-25T12:{i:02d}:00.000Z"} for i in range(50)]
n, out = run(many, s3)
assert n == 50 and "may be missing" in out, out

# S3 fails partway: the sync isn't broken and the marker stays at the last play that was written
before = ssm.store[lf.PLAY_LOG]
s3_bad = FakeS3(fail_after=0)
n, out = run([{**play(0, 99), "played_at": "2026-11-25T13:00:00.000Z"}], s3_bad)
assert n == 0 and "Couldn't write the play log" in out and ssm.store[lf.PLAY_LOG] == before

# pure helpers
assert s.play_key("2026-01-02T03:04:05Z") == "plays/2026/01/02/2026-01-02T03-04-05Z.json"
fresh, gap = s.new_plays([play(1)], None)
assert len(fresh) == 1 and not gap
print("play log ok")
