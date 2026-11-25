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
# same song played twice before a run (e.g. after downtime): archived once, at its first play
r=FR([5,3,5,9,5],[3]); s.run_job(r,{"from":"recent","to":D,"mode":"archive"},False); assert r.added==["spotify:track:5","spotify:track:9"], r.added
r=FR([5,3],[1]); s.run_job(r,{"from":"recent","to":D,"mode":"rolling"},False); assert r.replaced==["spotify:track:5","spotify:track:3","spotify:track:1"]
r=FR([5,3,9],[1]); s.run_job(r,{"from":"recent","to":D,"mode":"rolling","keep":2},False); assert r.replaced==["spotify:track:5","spotify:track:3"]
# --- last_played: playlist oldest -> newest by last play; replays move to the end, never duplicated
class FL(FR):
    def __init__(s, recent, dest): super().__init__(recent, dest); s.pl=[f"spotify:track:{x}" for x in dest]; s.removed=[]
    def playlist_uris(s, pid): return list(s.pl)
    def remove_from_playlist(s, pid, uris): s.removed += uris; s.pl=[u for u in s.pl if u not in uris]
    def add_to_playlist(s, pid, uris): s.added += uris; s.pl += uris
T=lambda *xs: [f"spotify:track:{x}" for x in xs]
LP={"from":"recent","to":D,"mode":"last_played"}
# recent newest-first: 7 (new) then 2 (replayed, was early in the list); 9 and 8 already at the tail
r=FL([7,2,9,8],[1,2,3,8,9]); r.recent=T(7,2,9,8)
s.run_job(r,LP,False); assert r.pl==T(1,3,8,9,2,7), r.pl; assert r.removed==T(2), r.removed
# running again changes nothing
r.added=[]; r.removed=[]; assert s.run_job(r,LP,False)==0 and r.added==[] and r.removed==[]
# a song played twice in the window counts at its latest play; dash spelling accepted
r=FL([5,3,5],[3,5]); r.recent=T(5,3,5); s.run_job(r,{**LP,"mode":"last-played"},False); assert r.pl==T(3,5), r.pl
r=FL([4,3,4,1],[1,2]); r.recent=T(4,3,4,1); s.run_job(r,LP,False); assert r.pl==T(2,1,3,4), r.pl
# plays falling out of the 50-play window don't cause churn
r=FL([9,8],[1,7,8]); r.recent=T(9,8); s.run_job(r,LP,False); assert r.pl==T(1,7,8,9) and r.removed==[]
# dry run
r=FL([2],[1,2,3]); r.recent=T(2); s.run_job(r,LP,True); assert r.pl==T(1,2,3) and r.added==[]
print("last_played ok")
for bad in [{"mode":"nope"},{"mode":"archive","keep":5},{"mode":"last_played","keep":5}]:
    try: s.run_job(FR([1],[]),{"from":"recent","to":D,**bad},False); assert False
    except ValueError: pass
print("mode ok")
