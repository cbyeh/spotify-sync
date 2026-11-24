"""Run: python tests/test_playback.py (no dependencies; uses fakes, never calls Spotify)."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import sys, types, copy
import spotify_sync as s

clock = [0.0]
s.time = types.SimpleNamespace(time=lambda: clock[0], sleep=lambda x: None)
s.SLEEP = lambda x: clock.__setitem__(0, clock[0] + x)

DWID = "37i9dQZEVXcFznARRTnOdi"; DW = f"spotify:playlist:{DWID}"
def tr(n): return {"type": "track", "uri": f"spotify:track:{n}", "is_local": False}

class Player:
    def __init__(self, n=30, manual=(), out_of_range="error", playing=False, active="phone", phone_volume=None):
        self.dw = [f"dw{i}" for i in range(n)]
        self.manual = list(manual)
        self.oor = out_of_range
        self.devices = {"pc": {"id": "pc", "type": "Computer", "name": "PC", "volume_percent": 70,
                               "supports_volume": True, "is_restricted": False},
                        "phone": {"id": "phone", "type": "Smartphone", "name": "Phone", "volume_percent": 50,
                                  "supports_volume": False, "is_restricted": False}}
        self.active = active
        self.st = {"context": "spotify:playlist:MINE", "item": "mine7", "progress": 12345,
                   "shuffle": True, "repeat": "context", "playing": playing}
        self.log = []; self.plays = 0
    def snapshot(self):
        return (self.active, {k: v for k, v in self.st.items() if k != "pos"}, {k: d["volume_percent"] for k, d in self.devices.items()})
    def state(self):
        if not self.active: return {}
        return {"is_playing": self.st["playing"], "shuffle_state": self.st["shuffle"], "repeat_state": self.st["repeat"],
                "progress_ms": self.st["progress"], "device": dict(self.devices[self.active]),
                "context": {"uri": self.st["context"]} if self.st["context"] else None, "item": tr(self.st["item"])}
    def request(self, method, path, body=None):
        self.log.append((method, path, copy.deepcopy(body)))
        p, _, qs = path.partition("?"); q = dict(x.split("=") for x in qs.split("&") if x)
        if (method, p) == ("GET", "/me/player"): return self.state()
        if (method, p) == ("GET", "/me/player/devices"): return {"devices": list(self.devices.values())}
        if (method, p) == ("PUT", "/me/player"): self.active = body["device_ids"][0]; return {}
        if (method, p) == ("PUT", "/me/player/volume"):
            d = self.devices[q["device_id"]]
            if not d["supports_volume"]: raise s.SpotifyError("403 VOLUME_CONTROL_DISALLOW")
            d["volume_percent"] = int(q["volume_percent"]); return {}
        if (method, p) == ("PUT", "/me/player/shuffle"): self.st["shuffle"] = q["state"] == "true"; return {}
        if (method, p) == ("PUT", "/me/player/repeat"): self.st["repeat"] = q["state"]; return {}
        if (method, p) == ("PUT", "/me/player/pause"): self.st["playing"] = False; return {}
        if (method, p) == ("PUT", "/me/player/play"):
            self.plays += 1
            if self.devices[self.active]["supports_volume"]:
                assert self.devices[self.active]["volume_percent"] == 0 or body.get("context_uri") != DW, "played DW unmuted!"
            if body.get("context_uri") == DW:
                assert self.st["shuffle"] is False, "shuffle still on"
                pos = body["offset"]["position"]
                if pos >= len(self.dw):
                    if self.oor == "error": raise s.SpotifyError("400 offset out of range")
                    pos = 0   # some clients restart instead
                self.st.update(context=DW, item=self.dw[pos], playing=True, progress=0, pos=pos)
            else:
                item = body["offset"]["uri"].split(":")[-1] if "offset" in body else body["uris"][0].split(":")[-1]
                self.st.update(context=body.get("context_uri"), item=item, playing=True, progress=body.get("position_ms", 0))
            return {}
        if (method, p) == ("GET", "/me/player/currently-playing"):
            st = self.state(); return {"is_playing": st.get("is_playing"), "context": st.get("context"), "item": st.get("item")}
        if (method, p) == ("GET", "/me/player/queue"):
            pos = self.st.get("pos", 0)
            nxt = self.manual + self.dw[pos + 1:] + [f"auto{i}" for i in range(40)]
            return {"queue": [tr(n) for n in nxt[:20]]}
        raise AssertionError(path)

def run(player, **kw):
    sp = s.Spotify("A"); sp.request = player.request
    return s.read_by_playback(sp, DWID, **kw)

# 1) normal 30-track DW, idle desktop, phone was the active device
p = Player(); before = p.snapshot()
got = run(p)
assert got == [f"spotify:track:dw{i}" for i in range(30)], got
assert p.snapshot() == before, (p.snapshot(), before)
print("30 tracks, starts:", p.plays - 1, "(+1 restore)")
# 2) out-of-range offset restarts at 0 instead of erroring
p = Player(out_of_range="restart"); got = run(p); assert len(got) == 30 and got[-1].endswith("dw29")
# 3) songs queued by hand -> one-by-one fallback still exact
p = Player(manual=["mq1", "mq2"]); before = p.snapshot(); got = run(p)
assert got == [f"spotify:track:dw{i}" for i in range(30)], got; assert p.snapshot() == before
print("with hand-queued songs, starts:", p.plays - 1)
# 4) other lengths
for n in (1, 20, 21, 22, 50):
    p = Player(n=n); assert len(run(p)) == n, n
# 5) busy / no computer
class Advancing(Player):
    def state(self):
        st = super().state()
        if self.st["playing"]: self.st["progress"] += 1000
        return st
p = Advancing(playing=True)
try: run(p); assert False
except s.PlaybackUnavailable as e: assert "playing" in str(e)
p = Player(); del p.devices["pc"]
try: run(p); assert False
except s.PlaybackUnavailable as e: assert "computer" in str(e)
# 6) nothing was active before (no session)
p = Player(active=None); got = run(p); assert len(got) == 30
# 7) named device
p = Player(); assert len(run(p, device_name="PC")) == 30
print("playback ok")

# --- weekly job logic
class St(dict):
    def set(self, k, v): self[k] = v
added = []
def mk(player, dest):
    sp = s.Spotify("A"); sp.request = player.request
    sp.playlist_uris = lambda pid: list(dest)
    sp.add_to_playlist = lambda pid, u: (added.extend(u), dest.extend(u))
    return sp
job = {"name": "DW full", "from": DWID, "to": "41JXOe9PZ9g6v2PUW687l6", "via": "playback"}
st = St(); dest = [f"spotify:track:dw{i}" for i in range(5)]; mails = []
n = s.run_playback_job(mk(Player(), dest), job, False, st, notify=lambda *a: mails.append(a))
assert n == 25 and len(st) == 1
n = s.run_playback_job(mk(Player(), dest), job, False, st)          # same week: no playback at all
assert n == 0
# not refreshed yet: nothing new -> not marked done; final -> marked done, no email
st2 = St(); full = [f"spotify:track:dw{i}" for i in range(30)]
assert s.run_playback_job(mk(Player(), list(full)), job, False, st2) == 0 and not st2
s.run_playback_job(mk(Player(), list(full)), job, False, st2, final=True); assert st2
# unavailable on final attempt -> email
p = Advancing(playing=True); st3 = St()
s.run_playback_job(mk(p, []), job, False, st3, final=True, notify=lambda *a: mails.append(a))
assert len(mails) == 1 and "couldn't copy" in mails[0][0] and not st3
# normal minute runs ignore playback jobs and vice versa
assert s.is_playback_job(job) and not s.is_playback_job({"from": "liked", "to": "x"})
print("weekly job ok")

# --- live-run regressions
class SlowPlayer(Player):
    """Transfer takes a few polls to land; until then GET /me/player is empty (like real Spotify)."""
    def __init__(self, delay=3, **kw):
        super().__init__(active=None, **kw); self.delay = delay; self.pending = None
    def request(self, method, path, body=None):
        if (method, path.split("?")[0]) == ("PUT", "/me/player"):
            self.pending = body["device_ids"][0]; self.left = self.delay; return {}
        if (method, path.split("?")[0]) == ("GET", "/me/player") and self.pending:
            if self.left > 0: self.left -= 1; return {}
            self.active, self.pending = self.pending, None
        return super().request(method, path, body)
p = SlowPlayer(); p.st.update(shuffle=True, repeat="track")
got = run(p); assert len(got) == 30
assert p.st["shuffle"] is True and p.st["repeat"] == "track" and p.st["item"] == "mine7" and p.devices["pc"]["volume_percent"] == 70, p.st
p = SlowPlayer(delay=10**6); p.st.update(shuffle=True)
try: run(p); assert False
except s.PlaybackUnavailable as e: assert "nothing was changed" in str(e)
assert p.plays == 0 and p.st["shuffle"] is True and p.devices["pc"]["volume_percent"] == 70
p = Player(n=80); before = p.snapshot()
try: run(p); assert False
except s.PlaybackUnavailable as e: assert "not trusting" in str(e)
assert p.snapshot() == before
print("live regressions ok")

# --- second live run: endless context + async commands
p = Player(n=220); before = p.snapshot()
got = run(p, take=30)
assert got == [f"spotify:track:dw{i}" for i in range(30)], len(got); assert p.snapshot() == before
print("take=30 on an endless playlist, starts:", p.plays - 1)

class LaggyPlayer(Player):
    """Like real Spotify: a play lands a couple of reads later; a pause sent before it lands is
    lost; when a context loads, shuffle/repeat reset to off."""
    def __init__(self, **kw):
        super().__init__(**kw); self.pending = None
    def request(self, method, path, body=None):
        m, p_ = method, path.split("?")[0]
        if (m, p_) == ("PUT", "/me/player/play") and body.get("context_uri") != DW:
            self.plays += 1
            self.pending = [2, body]; return {}
        if m == "GET" and self.pending:
            self.pending[0] -= 1
            if self.pending[0] <= 0:
                b = self.pending[1]; self.pending = None
                item = b["offset"]["uri"].split(":")[-1] if "offset" in b else b["uris"][0].split(":")[-1]
                self.st.update(context=b.get("context_uri"), item=item, playing=True,
                               progress=b.get("position_ms", 0), shuffle=False, repeat="off")
        if (m, p_) == ("PUT", "/me/player/pause") and self.pending:
            return {}   # lost: nothing is playing yet
        return super().request(method, path, body)
p = LaggyPlayer(); before = p.snapshot()
got = run(p, take=30); assert len(got) == 30
assert p.snapshot() == before, (p.snapshot(), before)
print("async restore ok")

class NeverPauses(Player):
    def request(self, method, path, body=None):
        if path.startswith("/me/player/pause"): return {}
        return super().request(method, path, body)
p = NeverPauses()
run(p, take=30)
assert p.devices["pc"]["volume_percent"] == 0, "must stay muted if it can't confirm paused"
print("stays muted ok")

# --- stale "playing" state (client stopped syncing)
p = Player(playing=True)
try: run(p, take=30); assert False
except s.PlaybackUnavailable as e: assert "out of sync" in str(e), e
print("stale state ok")
