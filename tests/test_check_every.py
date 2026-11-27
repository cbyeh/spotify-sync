"""Run: python tests/test_check_every.py (no dependencies; fakes only).

`check_every = N` (minutes): a job's source is looked at (= Spotify requests) at most every N
minutes; skipped runs make no request at all; a full check ignores it; a failed check is retried
on the next run; dry runs don't move the clock; jobs without it are checked every run."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spotify_sync as s  # noqa: E402

D = "6JqJYMSccCzSFTB0XMFGXZ"
LIKED = {"from": "liked", "to": D, "check_every": 30}


class State(dict):
    def set(self, k, v): self[k] = v


clock = [1_000_000.0]
s.time.time = lambda: clock[0]
calls = {"marker": 0, "sync": 0}
fail = [False]


def fake_marker(sp, job):
    calls["marker"] += 1
    if fail[0]:
        raise s.SpotifyError("boom")
    return "m1"


def fake_sync(sp, job, dry_run):
    calls["sync"] += 1
    return 0


s.change_marker = fake_marker
s.sync_job = fake_sync
st = State()


def run(minutes_later=0, **kw):
    clock[0] += minutes_later * 60
    s.run_job(None, kw.pop("job", LIKED), kw.pop("dry_run", False), st, **kw)


run()                          # first run: checks (marker + sync, since nothing is stored yet)
assert calls == {"marker": 1, "sync": 1}, calls
for _ in range(5):             # the next 25 minutes on a 5-minute schedule: no requests at all
    run(5)
assert calls["marker"] == 1, calls
run(5)                         # 30 minutes after the last check: checks again (marker unchanged, no sync)
assert calls == {"marker": 2, "sync": 1}, calls
run(5); assert calls["marker"] == 2
run(0, full=True); assert calls["marker"] == 3 and calls["sync"] == 2   # the daily full check ignores it

# a failed check doesn't move the clock: retried on the very next run
clock[0] += 31 * 60
fail[0] = True
try:
    run(0)
except s.SpotifyError:
    pass
fail[0] = False
run(5); assert calls["marker"] == 5, calls

# dry runs check but don't move the clock
before = st["liked-" + D + "-checked"]
run(31, dry_run=True); assert st["liked-" + D + "-checked"] == before

# without check_every: every run checks
n = calls["marker"]
run(5, job={"from": "liked", "to": D}); run(5, job={"from": "liked", "to": D})
assert calls["marker"] == n + 2

# validation
for bad in (0, -5, "30", True):
    try:
        s.check_every_seconds({"check_every": bad}); assert False, bad
    except ValueError:
        pass
assert s.check_every_seconds({"check_every": 30}) == 1800
print("check_every ok")
