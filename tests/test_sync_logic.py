"""Run: python tests/test_sync_logic.py (no dependencies; uses fakes, never calls Spotify)."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spotify_sync as s

def t(i, key="item"): return {key: {"type":"track","uri":f"spotify:track:{i}","is_local":False}}
class Fake(s.Spotify):
    def __init__(self, liked, dest, src=None, src_err=None):
        self.liked, self.dest, self.src, self.src_err, self.added = liked, dest, src, src_err, []
    def pages(self, path):
        if path.startswith("/me/tracks"):
            for i in range(0, len(self.liked), 50): yield [t(x,"track") for x in self.liked[i:i+50]]
        elif "DDDDDDDDDDDDDDDDDDDDDD" in path:
            for i in range(0, len(self.dest), 100): yield [t(x) for x in self.dest[i:i+100]] + [{"item":{"type":"episode","uri":"e"}}]
        else:
            if self.src_err: raise s.SpotifyError("GET x -> 404: nope")
            yield [t(x) for x in self.src]
    def add_to_playlist(self, pid, uris): self.added += uris

D = "https://open.spotify.com/playlist/DDDDDDDDDDDDDDDDDDDDDD?si=abc"
# liked newest-first: 200..1 ; dest already has 1..150
f = Fake(list(range(200,0,-1)), list(range(1,151)))
n = s.run_job(f, {"from":"liked","to":D}, False)
assert f.added == [f"spotify:track:{i}" for i in range(151,201)], f.added[:3]
# no-op second run
f2 = Fake(list(range(200,0,-1)), list(range(1,201))); assert s.run_job(f2, {"from":"liked","to":D}, False)==0
# backfill with a gap deep in history
f3 = Fake(list(range(200,0,-1)), [x for x in range(1,201) if x!=5]); s.run_job(f3, {"from":"liked","to":D,"backfill":True}, False); assert f3.added==["spotify:track:5"]
f4 = Fake(list(range(200,0,-1)), [x for x in range(1,201) if x!=5]); s.run_job(f4, {"from":"liked","to":D}, False); assert f4.added==[]
# playlist source with dupes
f5 = Fake([], [1,2], src=[1,3,3,4]); s.run_job(f5, {"from":"spotify:playlist:SSSSSSSSSSSSSSSSSSSSSS","to":D}, False); assert f5.added==["spotify:track:3","spotify:track:4"]
# dry run
f6 = Fake([], [1], src=[2]); s.run_job(f6, {"from":"SSSSSSSSSSSSSSSSSSSSSS","to":D}, True); assert f6.added==[]
# DW error
try: s.run_job(Fake([], [], src_err=True), {"from":"SSSSSSSSSSSSSSSSSSSSSS","to":D}, False); assert False
except s.SpotifyError as e: assert "Discover Weekly" in str(e)
print("all ok")
# --- recent
class FR(Fake):
    def __init__(s, recent, dest): super().__init__([], dest); s.recent=[f"spotify:track:{x}" for x in recent]; s.replaced=None
    def recently_played(s, only_from=None): return s.recent
    def replace_playlist(s, pid, uris): s.replaced=uris
r=FR([5,3,5,9],[3,1,2]); s.run_job(r,{"from":"recent","to":D,"keep":4},False)
assert r.replaced==["spotify:track:5","spotify:track:3","spotify:track:9","spotify:track:1"], r.replaced
r=FR([3,1],[3,1,2]); assert s.run_job(r,{"from":"recent","to":D,"keep":3},False)==0 and r.replaced is None
r=FR([5,3,9],[3]); s.run_job(r,{"from":"recent","to":D},False); assert r.added==["spotify:track:9","spotify:track:5"], r.added
print("recent ok")
# --- mode option
r=FR([5,3,9],[3]); s.run_job(r,{"from":"recent","to":D,"mode":"archive"},False); assert r.added==["spotify:track:9","spotify:track:5"]
r=FR([5,3],[1]); s.run_job(r,{"from":"recent","to":D,"mode":"rolling"},False); assert r.replaced==["spotify:track:5","spotify:track:3","spotify:track:1"]
r=FR([5,3,9],[1]); s.run_job(r,{"from":"recent","to":D,"mode":"rolling","keep":2},False); assert r.replaced==["spotify:track:5","spotify:track:3"]
for bad in [{"mode":"nope"},{"mode":"archive","keep":5}]:
    try: s.run_job(FR([1],[]),{"from":"recent","to":D,**bad},False); assert False
    except ValueError: pass
print("mode ok")
