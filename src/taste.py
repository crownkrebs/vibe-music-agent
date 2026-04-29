"""Taste DNA — built from the user's actual liked songs."""
import json
from .vibe import mean, percentile, profile_from_features, FEATURES


class TasteDNA:
    def __init__(self, spotify_client, db):
        self.sp = spotify_client
        self.db = db
        self.profile = None
        self.artist_counts = {}
        self.genre_weights = {}  # {genre: weighted_count}
        self._load_cached()

    def _load_cached(self):
        # wire the DB into the Spotify client so artist_genres_batch can
        # read/write the persistent artist_genres table.
        try:
            if hasattr(self.sp, "attach_db"):
                self.sp.attach_db(self.db)
        except Exception:
            pass
        snap = self.db.latest_taste_snapshot()
        if snap:
            self.profile = snap["profile"]
            self.artist_counts = self.profile.get("artist_counts", {})
            self.genre_weights = self.profile.get("genre_weights", {}) or {}

    def build(self, progress_cb=None):
        """Full taste analysis. Fetches all liked songs, gets features, builds profile."""
        if progress_cb:
            progress_cb("Fetching liked songs...")
        liked = self.sp.get_all_liked_songs()

        if not liked:
            raise RuntimeError("No liked songs found — can't build taste DNA")

        track_entries = []
        for item in liked:
            t = item.get("track")
            if not t or not t.get("id"):
                continue
            track_entries.append({
                "id": t["id"],
                "name": t["name"],
                "artist": t["artists"][0]["name"] if t["artists"] else "",
                "artists": [a["name"] for a in t.get("artists", [])],
                "added_at": item.get("added_at"),
                "popularity": t.get("popularity", 0),
                "explicit": t.get("explicit", False),
            })

        if progress_cb:
            progress_cb(f"Fetching audio features for {len(track_entries)} tracks...")

        features = self.sp.batch_audio_features([t["id"] for t in track_entries])

        valid = []
        for track, feat in zip(track_entries, features):
            if feat:
                valid.append((track, feat))

        if not valid:
            # features API may be deprecated for this app — build minimal profile
            profile = self._minimal_profile(track_entries)
        else:
            profile = profile_from_features([f for _, f in valid])
            profile["recent_direction"] = self._recent_trend(valid)

        # artist counts — always available
        artists = {}
        for t in track_entries:
            for a in t["artists"]:
                artists[a] = artists.get(a, 0) + 1

        profile["artist_counts"] = artists
        profile["total_analyzed"] = len(track_entries)
        profile["features_available"] = len(valid) > 0

        # cache tracks
        for track, feat in zip(track_entries, features):
            try:
                self.db.cache_track(
                    track["id"], track["name"], track["artist"],
                    track["artists"], "", None, feat or {},
                )
            except Exception:
                pass

        self.profile = profile
        self.artist_counts = artists

        # genre profile — primary signal since audio_features is dead at API level
        try:
            if progress_cb:
                progress_cb("Building genre profile from top artists...")
            self.build_genre_profile()
            profile["genre_weights"] = self.genre_weights
        except Exception as e:
            profile["genre_weights"] = {}
            if progress_cb:
                progress_cb(f"Genre profile skipped: {e}")

        self.db.save_taste_snapshot(profile)
        if progress_cb:
            progress_cb(f"Done — {len(valid)} tracks profiled")
        return profile

    # ---------- genre profile ----------
    def build_genre_profile(self, top_n=100):
        """Fetch genres for top_n artists (by like count) and build a weighted
        genre map. Each genre accumulates artist_counts[artist] across all
        top artists tagged with it. Result stored on self.genre_weights."""
        if not self.artist_counts:
            self.genre_weights = {}
            return {}
        top = sorted(self.artist_counts.items(), key=lambda x: -x[1])[:top_n]
        # resolve artist name -> id via search (limit=1). This call is cheap
        # and results get cached in spotipy's OAuth session.
        name_to_id = {}
        for name, _cnt in top:
            try:
                items = self.sp.search_artist(name, limit=1)
            except Exception:
                items = []
            if items and items[0].get("id"):
                name_to_id[name] = items[0]["id"]
        ids = list(name_to_id.values())
        # batch fetch
        try:
            id_to_genres = self.sp.artist_genres_batch(ids)
        except Exception:
            id_to_genres = {}
        weights = {}
        for name, cnt in top:
            aid = name_to_id.get(name)
            if not aid:
                continue
            for g in id_to_genres.get(aid, []) or []:
                g = (g or "").strip().lower()
                if not g:
                    continue
                weights[g] = weights.get(g, 0) + float(cnt)
        self.genre_weights = weights
        return weights

    def genre_affinity(self, genres_list):
        """How strongly does this candidate's genres overlap with the user's
        top genres? Returns 0..1. Uses substring-aware matching so 'trap'
        matches 'trap latino' / 'atl hip hop' etc."""
        if not genres_list:
            return 0.0
        if not self.genre_weights:
            return 0.5  # neutral when no profile is built yet
        cand = [(g or "").strip().lower() for g in genres_list if g]
        cand = [g for g in cand if g]
        if not cand:
            return 0.0
        top_sum = max(sum(self.genre_weights.values()), 1.0)
        # max possible single-hit weight (normaliser for score)
        top_weight = max(self.genre_weights.values()) if self.genre_weights else 1.0
        best_hit = 0.0
        total_hit = 0.0
        for c in cand:
            for g, w in self.genre_weights.items():
                # exact, substring either way, or shared tokens (2+)
                if c == g:
                    match_w = w
                elif c in g or g in c:
                    match_w = w * 0.75
                else:
                    c_toks = set(c.split())
                    g_toks = set(g.split())
                    shared = c_toks & g_toks
                    if len(shared) >= 2:
                        match_w = w * 0.5
                    elif len(shared) == 1 and len(next(iter(shared))) >= 4:
                        match_w = w * 0.35
                    else:
                        continue
                total_hit += match_w
                if match_w > best_hit:
                    best_hit = match_w
        # blend: 70% best-single-match normalised by top_weight,
        # 30% cumulative normalised by total catalog weight
        single = min(1.0, best_hit / max(top_weight, 1.0))
        cumulative = min(1.0, total_hit / max(top_sum * 0.4, 1.0))
        return max(0.0, min(1.0, single * 0.7 + cumulative * 0.3))

    def _minimal_profile(self, tracks):
        """Fallback when audio_features is unavailable (API-level 403).
        Scoring gracefully falls back to metadata signals (genre, artist
        familiarity, popularity, era) when features aren't populated."""
        return {
            "energy": {"mean": 0.6, "p10": 0.4, "p90": 0.85, "p25": 0.5, "p75": 0.75},
            "valence": {"mean": 0.5, "p10": 0.25, "p90": 0.8, "p25": 0.4, "p75": 0.7},
            "danceability": {"mean": 0.65, "p10": 0.45, "p90": 0.85, "p25": 0.55, "p75": 0.78},
            "tempo": {"mean": 115, "p10": 85, "p90": 155, "p25": 95, "p75": 135},
            "acousticness": {"mean": 0.25, "p10": 0.02, "p90": 0.7, "p25": 0.05, "p75": 0.4},
            "features_available": False,
        }

    def _recent_trend(self, valid):
        """Compare last ~10% of likes vs whole library to detect direction."""
        sorted_by_date = sorted(
            valid, key=lambda tf: tf[0].get("added_at") or "", reverse=True,
        )
        n = len(sorted_by_date)
        recent_n = max(10, n // 10)
        recent = sorted_by_date[:recent_n]
        trend = {}
        for feat in ["energy", "valence", "danceability", "acousticness"]:
            r = mean([f.get(feat) for _, f in recent if f.get(feat) is not None])
            a = mean([f.get(feat) for _, f in valid if f.get(feat) is not None])
            trend[f"{feat}_shift"] = round(r - a, 3)
        rt = mean([f.get("tempo") for _, f in recent if f.get("tempo")])
        at = mean([f.get("tempo") for _, f in valid if f.get("tempo")])
        trend["tempo_shift"] = round(rt - at, 1)
        trend["recent_top_artists"] = self._top_artists_in(recent, 10)
        return trend

    def _top_artists_in(self, valid_subset, n=10):
        counts = {}
        for track, _ in valid_subset:
            for a in track.get("artists", []):
                counts[a] = counts.get(a, 0) + 1
        return sorted(counts.items(), key=lambda x: -x[1])[:n]

    # ---------- scoring ----------
    def score(self, track_features, target=None):
        """Score a track's audio features (0-1). target is a playlist profile dict or None.
        When features are missing (API-deprecated case) returns a neutral 0.5 so
        the metadata-based gate in recommend.py remains the decisive signal."""
        if not track_features:
            # features unavailable — let metadata signals decide
            return 0.5
        # if TasteDNA was built without features, the center is synthetic;
        # still return a mild signal rather than pretending to score.
        if self.profile and self.profile.get("features_available") is False:
            return 0.5
        if not self.profile:
            return 0.5

        tgt = target or self._center()
        diffs = {
            "energy": abs(track_features.get("energy", 0.5) - tgt.get("energy", 0.55)),
            "valence": abs(track_features.get("valence", 0.5) - tgt.get("valence", 0.55)),
            "danceability": abs(track_features.get("danceability", 0.5) - tgt.get("danceability", 0.6)),
            "acousticness": abs(track_features.get("acousticness", 0.3) - tgt.get("acousticness", 0.3)),
        }
        t_track = track_features.get("tempo", 110) or 110
        t_tgt = tgt.get("tempo", 115)
        d_tempo = min(
            abs(t_track - t_tgt),
            abs(t_track - t_tgt * 2),
            abs(t_track * 2 - t_tgt),
        ) / 200.0

        distance = (
            diffs["energy"] * 0.28 +
            diffs["valence"] * 0.20 +
            diffs["danceability"] * 0.22 +
            d_tempo * 0.15 +
            diffs["acousticness"] * 0.15
        )
        return max(0.0, min(1.0, 1.0 - distance * 1.8))

    def artist_familiarity(self, artist_name):
        """How familiar is this artist? 0 = unknown, 1 = top artist."""
        if not artist_name or not self.artist_counts:
            return 0.0
        count = self.artist_counts.get(artist_name, 0)
        if count == 0:
            return 0.0
        max_count = max(self.artist_counts.values()) if self.artist_counts else 1
        return min(1.0, count / max(1, max_count))

    def top_artists(self, n=20):
        return sorted(self.artist_counts.items(), key=lambda x: -x[1])[:n]

    def _center(self):
        """Get a dict of mean values from current profile."""
        if not self.profile:
            return {}
        out = {}
        for key in ["energy", "valence", "danceability", "acousticness", "tempo"]:
            v = self.profile.get(key)
            if isinstance(v, dict):
                out[key] = v.get("mean")
            else:
                out[key] = v
        return out

    def center(self):
        return self._center()

    def summary(self):
        """Short human summary of taste profile."""
        c = self._center()
        trend = self.profile.get("recent_direction", {}) if self.profile else {}
        top = self.top_artists(10)
        return {
            "center": c,
            "recent_direction": trend,
            "top_artists": top,
            "total": self.profile.get("total_analyzed", 0) if self.profile else 0,
            "features_available": self.profile.get("features_available", True) if self.profile else True,
        }
