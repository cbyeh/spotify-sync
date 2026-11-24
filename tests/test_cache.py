"""Run: python tests/test_cache.py (no dependencies; uses fakes, never calls Spotify).

The persisted playlist cache: a cold start must not re-read a big playlist whose snapshot_id
hasn't changed, our own adds must keep the cache current, and outside edits must be noticed."""
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spotify_sync as s

clock = [1_000_000.0]
s.time = types.SimpleNamespace(time=lambda: clock[0], sleep=lambda x: None)


class FakeSpotifyPlaylist:
    """One playlist on Spotify's side: 250 tracks, a snapshot that changes on every edit."""

    def __init__(self, n=250):
        self.uris = [f"spotify:track:t{i}" for i in range(n)]
        self.version = 1
        self.page_reads = 0

    def request(self, method, path, body=None):
        if path.endswith("fields=snapshot_id"):
            return {"snapshot_id": f"snap{self.version}"}
        if method == "GET" and "/items" in path:
            self.page_reads += 1
            off = int(path.split("offset=")[1]) if "offset=" in path else 0
            nxt = path.split("&offset")[0] + f"&offset={off + 100}" if off + 100 < len(self.uris) else None
            return {"items": [{"item": {"type": "track", "uri": u}} for u in self.uris[off:off + 100]], "next": nxt}
        if method == "POST":
            self.uris += body["uris"]; self.version += 1
            return {"snapshot_id": f"snap{self.version}"}
        raise AssertionError(path)

    def edit_elsewhere(self, uri):
        self.uris.append(uri); self.version += 1


class MemStore(dict):
    def get(self, pid): return dict.get(self, pid)
    def put(self, pid, entry): self[pid] = entry
    def delete(self, pid): self.pop(pid, None)


def session(remote, store):
    sp = s.Spotify("A"); sp.request = remote.request; sp.store = store
    return sp


remote, store = FakeSpotifyPlaylist(), MemStore()
PID = "WWWWWWWWWWWWWWWWWWWWWW"

# 1) first ever read: pages through and saves
assert len(session(remote, store).playlist_uris(PID)) == 250 and remote.page_reads == 3 and PID in store

# 2) cold start (new session, same store), nothing changed: zero page reads
remote.page_reads = 0
assert len(session(remote, store).playlist_uris(PID)) == 250 and remote.page_reads == 0

# 3) our own add keeps the cache current: next cold start still reads nothing
sp = session(remote, store); sp.playlist_uris(PID); sp.add_to_playlist(PID, ["spotify:track:new1"])
remote.page_reads = 0
got = session(remote, store).playlist_uris(PID)
assert got[-1] == "spotify:track:new1" and len(got) == 251 and remote.page_reads == 0

# 4) someone edits the playlist in the app: snapshot changes -> re-read, sees the edit
remote.edit_elsewhere("spotify:track:manual")
got = session(remote, store).playlist_uris(PID)
assert "spotify:track:manual" in got and remote.page_reads == 3

# 5) weekly safety net: even with an unchanged snapshot, re-read after 7 days
remote.page_reads = 0
clock[0] += s.FULL_REFRESH_SECONDS + 1
session(remote, store).playlist_uris(PID)
assert remote.page_reads == 3

# 6) a broken store never fails a run (falls back to reading Spotify)
class Broken:
    def get(self, pid): raise RuntimeError("s3 down")
    def put(self, pid, e): raise RuntimeError("s3 down")
    def delete(self, pid): raise RuntimeError("s3 down")
remote.page_reads = 0
assert len(session(remote, Broken()).playlist_uris(PID)) == 252 and remote.page_reads == 3

# 7) no duplicates: the dedupe check sees cached contents, including our earlier adds
sp = session(remote, store)
existing = set(sp.playlist_uris(PID))
assert "spotify:track:new1" in existing and "spotify:track:manual" in existing
print("cache ok")
