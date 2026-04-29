"""Spotify API wrapper. All external music calls go through here."""
import time
import spotipy
from spotipy.oauth2 import SpotifyOAuth


SCOPES = [
    "user-library-read",
    "playlist-modify-public",
    "playlist-modify-private",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-read-recently-played",
    "user-top-read",
]


class SpotifyError(Exception):
    pass


class SpotifyClient:
    def __init__(self, config):
        self.config = config
        auth = SpotifyOAuth(
            client_id=config["spotify_client_id"],
            client_secret=config["spotify_client_secret"],
            redirect_uri=config.get("spotify_redirect_uri", "http://127.0.0.1:8888/callback"),
            scope=" ".join(SCOPES),
            cache_path=".spotify_cache",
            open_browser=True,
        )
        self.sp = spotipy.Spotify(auth_manager=auth, requests_timeout=20, retries=3)
        self._me = None
        # in-process caches (avoid refetching per session)
        self._artist_genre_cache = {}   # artist_id -> [genres]
        self._artist_name_cache = {}    # artist_id -> name
        self._related_artists_dead = False  # flips True if endpoint returns deprecated

    # ---------- identity ----------
    def me(self):
        if not self._me:
            self._me = self.sp.current_user()
        return self._me

    # ---------- liked songs ----------
    def get_all_liked_songs(self, limit=None):
        songs, offset = [], 0
        while True:
            try:
                results = self.sp.current_user_saved_tracks(limit=50, offset=offset)
            except Exception as e:
                raise SpotifyError(f"liked songs fetch failed at offset {offset}: {e}")
            items = results.get("items", [])
            if not items:
                break
            songs.extend(items)
            offset += 50
            if limit and len(songs) >= limit:
                songs = songs[:limit]
                break
            time.sleep(0.05)
        return songs

    def recent_liked_ids(self, count=30):
        try:
            r = self.sp.current_user_saved_tracks(limit=min(50, count))
            return [t["track"]["id"] for t in r.get("items", []) if t.get("track")]
        except Exception:
            return []

    # ---------- audio features ----------
    def batch_audio_features(self, track_ids):
        """Get audio features for tracks (up to 100 per call). Returns list
        aligned with input (None for failures)."""
        results = []
        ids = [t for t in track_ids if t]
        for i in range(0, len(ids), 100):
            batch = ids[i:i + 100]
            try:
                feats = self.sp.audio_features(batch)
                results.extend(feats or [None] * len(batch))
            except Exception:
                results.extend([None] * len(batch))
            time.sleep(0.1)
        return results

    # ---------- recommendations ----------
    def get_recommendations(self, seed_tracks=None, seed_genres=None, seed_artists=None,
                            limit=30, **audio_params):
        seed_tracks = [s for s in (seed_tracks or []) if s][:5]
        seed_genres = [g for g in (seed_genres or []) if g][:5]
        seed_artists = [a for a in (seed_artists or []) if a][:5]

        total = len(seed_tracks) + len(seed_genres) + len(seed_artists)
        if total > 5:
            over = total - 5
            while over > 0 and seed_genres:
                seed_genres.pop()
                over -= 1
            while over > 0 and seed_artists:
                seed_artists.pop()
                over -= 1
            while over > 0 and seed_tracks:
                seed_tracks.pop()
                over -= 1

        params = {"limit": min(100, max(1, limit))}
        if seed_tracks:
            params["seed_tracks"] = seed_tracks
        if seed_genres:
            params["seed_genres"] = seed_genres
        if seed_artists:
            params["seed_artists"] = seed_artists
        for k, v in audio_params.items():
            if v is None:
                continue
            params[k] = v

        try:
            return self.sp.recommendations(**params)
        except Exception as e:
            if seed_genres and "seed_genres" in params:
                params.pop("seed_genres", None)
                try:
                    return self.sp.recommendations(**params)
                except Exception as e2:
                    raise SpotifyError(f"recommendations failed: {e2}") from e
            raise SpotifyError(f"recommendations failed: {e}") from e

    def get_genre_seeds(self):
        try:
            return self.sp.recommendation_genre_seeds().get("genres", [])
        except Exception:
            return []

    # ---------- tracks ----------
    def tracks(self, track_ids):
        out = []
        ids = [t for t in track_ids if t]
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            try:
                res = self.sp.tracks(batch)
                out.extend(res.get("tracks", []))
            except Exception:
                out.extend([None] * len(batch))
        return out

    def search_track(self, query, limit=5):
        try:
            r = self.sp.search(q=query, type="track", limit=limit)
            return r.get("tracks", {}).get("items", [])
        except Exception:
            return []

    def search_artist_title(self, artist, title, limit=5):
        """Field-qualified search. Returns up to `limit` track hits."""
        a = (artist or "").replace('"', '').strip()
        t = (title or "").replace('"', '').strip()
        if not (a and t):
            return []
        queries = [
            f'track:"{t}" artist:"{a}"',
            f'artist:"{a}" {t}',
            f'{a} {t}',
        ]
        seen = set()
        out = []
        for q in queries:
            for hit in self.search_track(q, limit=limit):
                hid = hit.get("id")
                if hid and hid not in seen:
                    seen.add(hid)
                    out.append(hit)
            if len(out) >= limit:
                break
        return out[:limit]

    def search_artist(self, name, limit=3):
        try:
            r = self.sp.search(q=name, type="artist", limit=limit)
            return r.get("artists", {}).get("items", [])
        except Exception:
            return []

    # ---------- playlists ----------
    def get_all_playlists(self):
        playlists, offset = [], 0
        me_id = self.me()["id"]
        while True:
            r = self.sp.current_user_playlists(limit=50, offset=offset)
            items = r.get("items", [])
            if not items:
                break
            for p in items:
                # only playlists user owns
                if p.get("owner", {}).get("id") == me_id:
                    playlists.append(p)
            offset += 50
            if len(items) < 50:
                break
        return playlists

    def get_playlist_tracks(self, playlist_id):
        tracks, offset = [], 0
        while True:
            r = self.sp.playlist_items(playlist_id, limit=100, offset=offset,
                                       fields="items(track(id,name,artists,album(name,images))),total")
            items = r.get("items", [])
            if not items:
                break
            for item in items:
                t = item.get("track")
                if not t or not t.get("id"):
                    continue
                tracks.append({
                    "id": t["id"],
                    "name": t["name"],
                    "artist": t["artists"][0]["name"] if t["artists"] else "",
                    "artists": [a["name"] for a in t.get("artists", [])],
                    "album": t.get("album", {}).get("name", ""),
                    "album_art": (t["album"]["images"][0]["url"]
                                  if t.get("album", {}).get("images") else None),
                })
            offset += 100
            if len(items) < 100:
                break
        return tracks

    def create_playlist(self, name, description="", public=False):
        me_id = self.me()["id"]
        p = self.sp.user_playlist_create(me_id, name, public=public, description=description)
        return p

    def playlist_add(self, playlist_id, track_ids):
        ids = [t for t in track_ids if t]
        for i in range(0, len(ids), 100):
            self.sp.playlist_add_items(playlist_id, ids[i:i + 100])

    def playlist_replace(self, playlist_id, track_ids):
        ids = [t for t in track_ids if t]
        if not ids:
            self.sp.playlist_replace_items(playlist_id, [])
            return
        self.sp.playlist_replace_items(playlist_id, ids[:100])
        for i in range(100, len(ids), 100):
            self.sp.playlist_add_items(playlist_id, ids[i:i + 100])

    def playlist_remove(self, playlist_id, track_ids):
        ids = [t for t in track_ids if t]
        for i in range(0, len(ids), 100):
            self.sp.playlist_remove_all_occurrences_of_items(playlist_id, ids[i:i + 100])

    def playlist_reorder(self, playlist_id, track_ids):
        """Replace playlist with given track_ids in given order (preserves ownership, overwrites order)."""
        self.playlist_replace(playlist_id, track_ids)

    # ---------- artists ----------
    def artist(self, artist_id):
        try:
            return self.sp.artist(artist_id)
        except Exception:
            return None

    def artists(self, artist_ids):
        out = []
        ids = [a for a in artist_ids if a]
        for i in range(0, len(ids), 50):
            try:
                out.extend(self.sp.artists(ids[i:i + 50]).get("artists", []))
            except Exception:
                out.extend([None] * len(ids[i:i + 50]))
        return out

    def artist_top_tracks(self, artist_id, market="US"):
        try:
            return self.sp.artist_top_tracks(artist_id, country=market).get("tracks", [])
        except Exception:
            return []

    def artist_related_artists(self, artist_id):
        """Spotify may have deprecated this endpoint. Returns [] and flips a
        circuit-breaker if it 404/403s so we stop calling it this session."""
        if self._related_artists_dead:
            return []
        try:
            r = self.sp.artist_related_artists(artist_id)
            return r.get("artists", []) if r else []
        except Exception:
            self._related_artists_dead = True
            return []

    # ---------- artist genres (cached) ----------
    def attach_db(self, db):
        """Optional: wire in a DB for persistent artist-genre caching."""
        self._db = db

    def _db_get_cached_genres(self, ids):
        db = getattr(self, "_db", None)
        if not db:
            return {}
        try:
            return db.get_cached_artist_genres(ids)
        except Exception:
            return {}

    def _db_put_genres(self, aid, name, genres):
        db = getattr(self, "_db", None)
        if not db:
            return
        try:
            db.cache_artist_genres(aid, name, genres)
        except Exception:
            pass

    def artist_genres(self, artist_id):
        """Fetch genres for a single artist id. Cached (memory + optional DB).
        Returns list."""
        if not artist_id:
            return []
        if artist_id in self._artist_genre_cache:
            return self._artist_genre_cache[artist_id]
        # check DB cache
        db_hit = self._db_get_cached_genres([artist_id]).get(artist_id)
        if db_hit is not None:
            self._artist_genre_cache[artist_id] = db_hit
            return db_hit
        try:
            a = self.sp.artist(artist_id)
        except Exception:
            a = None
        genres = (a or {}).get("genres") or []
        name = (a or {}).get("name") or ""
        self._artist_genre_cache[artist_id] = genres
        if name:
            self._artist_name_cache[artist_id] = name
        self._db_put_genres(artist_id, name, genres)
        return genres

    def artist_genres_batch(self, artist_ids):
        """Fetch genres for many artists in 50-size batches via sp.artists().
        Returns {artist_id: [genres]}. Cached (memory + optional DB)."""
        ids = [a for a in (artist_ids or []) if a]
        out = {}
        # dedupe input while preserving order
        seen_in = set()
        ids = [a for a in ids if not (a in seen_in or seen_in.add(a))]
        need = []
        for aid in ids:
            if aid in self._artist_genre_cache:
                out[aid] = self._artist_genre_cache[aid]
            else:
                need.append(aid)
        # DB cache sweep
        if need:
            db_hits = self._db_get_cached_genres(need)
            for aid, genres in db_hits.items():
                self._artist_genre_cache[aid] = genres
                out[aid] = genres
            need = [a for a in need if a not in db_hits]
        for i in range(0, len(need), 50):
            batch = need[i:i + 50]
            try:
                res = self.sp.artists(batch).get("artists", [])
            except Exception:
                res = []
            got_by_id = {}
            for a in res:
                if not a or not a.get("id"):
                    continue
                got_by_id[a["id"]] = a
            for aid in batch:
                a = got_by_id.get(aid)
                genres = (a or {}).get("genres") or []
                name = (a or {}).get("name") or ""
                self._artist_genre_cache[aid] = genres
                if name:
                    self._artist_name_cache[aid] = name
                out[aid] = genres
                self._db_put_genres(aid, name, genres)
            time.sleep(0.05)
        return out
