"""Playlist CRUD, profiles, clean, verify, health check.

Playlist profiles + descriptions are loaded from JSON at project root:
prefer ``playlists.json`` (user-local, gitignored), fall back to
``playlists.example.json`` (committed template). Each entry maps a playlist
NAME to a dict with ``description``, ``flow_style``, and audio targets
(``energy``, ``valence``, ``danceability``, ``tempo``, ``acousticness``).
"""
import json
from pathlib import Path

from .vibe import profile_from_features, center_of, vibe_distance, vibe_signature


_AUDIO_KEYS = ("energy", "valence", "danceability", "tempo", "acousticness")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_playlist_config():
    """Return (DEFAULT_PROFILES, PLAYLIST_DESCRIPTIONS, PLAYLIST_FLOW_STYLES).

    Loads playlists.json if present, else playlists.example.json. Silently
    returns empty dicts on any error so the app still boots."""
    candidates = [
        _PROJECT_ROOT / "playlists.json",
        _PROJECT_ROOT / "playlists.example.json",
    ]
    raw = None
    for path in candidates:
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                break
            except Exception:
                continue
    if not isinstance(raw, dict):
        return {}, {}, {}

    profiles, descriptions, flow_styles = {}, {}, {}
    for name, entry in raw.items():
        if name.startswith("_") or not isinstance(entry, dict):
            continue
        prof = {k: entry[k] for k in _AUDIO_KEYS if k in entry}
        if prof:
            profiles[name] = prof
        if entry.get("description"):
            descriptions[name] = str(entry["description"])
        if entry.get("flow_style"):
            flow_styles[name] = str(entry["flow_style"])
    return profiles, descriptions, flow_styles


DEFAULT_PROFILES, PLAYLIST_DESCRIPTIONS, PLAYLIST_FLOW_STYLES = _load_playlist_config()


class PlaylistManager:
    def __init__(self, spotify, db, flow):
        self.sp = spotify
        self.db = db
        self.flow = flow
        self._spotify_cache = None

    # ---------- profiles ----------
    def get_profile(self, playlist_name):
        if not playlist_name:
            return None
        evolved = self.db.get_playlist_profile(playlist_name)
        if evolved:
            return evolved
        return DEFAULT_PROFILES.get(playlist_name)

    def all_default_names(self):
        return list(DEFAULT_PROFILES.keys())

    # ---------- spotify directory ----------
    def list_from_spotify(self, force=False):
        if self._spotify_cache and not force:
            return self._spotify_cache
        playlists = self.sp.get_all_playlists()
        out = []
        for p in playlists:
            out.append({
                "id": p["id"],
                "name": p["name"],
                "total": p.get("tracks", {}).get("total", 0),
                "image": p["images"][0]["url"] if p.get("images") else None,
                "is_managed": p["name"] in DEFAULT_PROFILES,
            })
        self._spotify_cache = out
        return out

    def find_by_name(self, name):
        for p in self.list_from_spotify():
            if p["name"] == name:
                return p
        return None

    def find_or_create(self, name):
        p = self.find_by_name(name)
        if p:
            return p
        desc = PLAYLIST_DESCRIPTIONS.get(name, "")
        created = self.sp.create_playlist(name, description=desc, public=False)
        self._spotify_cache = None
        return {
            "id": created["id"],
            "name": created["name"],
            "total": 0,
            "image": None,
            "is_managed": name in DEFAULT_PROFILES,
        }

    # ---------- mutations ----------
    def add_tracks(self, playlist_name, track_ids, reorder_all_for_flow=True):
        p = self.find_or_create(playlist_name)
        if not track_ids:
            return {"added": 0, "playlist_id": p["id"]}

        existing = self.sp.get_playlist_tracks(p["id"])
        existing_ids = {t["id"] for t in existing}
        new_ids = [tid for tid in track_ids if tid not in existing_ids]

        if not new_ids:
            return {"added": 0, "playlist_id": p["id"], "already_present": True}

        if reorder_all_for_flow and existing:
            # rebuild full order with all tracks
            all_ids = [t["id"] for t in existing] + new_ids
            ordered = self._flow_order_ids(all_ids)
            self.sp.playlist_replace(p["id"], ordered)
        else:
            # just append in flow-ordered chunk
            if len(new_ids) > 2:
                new_ids = self._flow_order_ids(new_ids)
            self.sp.playlist_add(p["id"], new_ids)

        self._spotify_cache = None
        return {"added": len(new_ids), "playlist_id": p["id"]}

    def remove_tracks(self, playlist_name, track_ids):
        p = self.find_by_name(playlist_name)
        if not p:
            return {"removed": 0, "error": "playlist not found"}
        ids = [t for t in track_ids if t]
        if not ids:
            return {"removed": 0}
        self.sp.playlist_remove(p["id"], ids)
        self._spotify_cache = None
        return {"removed": len(ids), "playlist_id": p["id"]}

    def reorder_for_flow(self, playlist_name, style="smooth"):
        p = self.find_by_name(playlist_name)
        if not p:
            return {"error": "playlist not found"}
        tracks = self.sp.get_playlist_tracks(p["id"])
        if len(tracks) < 3:
            return {"reordered": 0, "reason": "not enough tracks"}
        ordered_ids = self._flow_order_ids([t["id"] for t in tracks], style=style)
        self.sp.playlist_replace(p["id"], ordered_ids)
        return {"reordered": len(ordered_ids), "playlist_id": p["id"], "style": style}

    def _flow_order_ids(self, track_ids, style="smooth"):
        if len(track_ids) < 3:
            return track_ids
        features = self.sp.batch_audio_features(track_ids)
        flow_input = []
        id_list = []
        for tid, feat in zip(track_ids, features):
            if feat:
                flow_input.append({"id": tid, **feat})
                id_list.append(tid)
            else:
                flow_input.append({"id": tid, "energy": 0.5, "valence": 0.5, "danceability": 0.6, "tempo": 110})
                id_list.append(tid)
        ordered = self.flow.order(flow_input, style=style)
        return [o["id"] for o in ordered]

    # ---------- clean / dedupe ----------
    def clean(self, playlist_name):
        p = self.find_by_name(playlist_name)
        if not p:
            return {"error": "playlist not found"}
        tracks = self.sp.get_playlist_tracks(p["id"])
        seen_ids, seen_keys, dupes = set(), set(), []
        for t in tracks:
            key = (t["name"].lower().strip(), t["artist"].lower().strip())
            if t["id"] in seen_ids or key in seen_keys:
                dupes.append(t)
            else:
                seen_ids.add(t["id"])
                seen_keys.add(key)

        if dupes:
            # remove ALL occurrences then re-add one each (preserves uniqueness)
            unique_ids = [t["id"] for t in tracks if t["id"] in seen_ids]
            self.sp.playlist_replace(p["id"], unique_ids)
            self._spotify_cache = None

        return {
            "duplicates_removed": len(dupes),
            "remaining": len(tracks) - len(dupes),
            "duplicate_tracks": [{"name": t["name"], "artist": t["artist"]} for t in dupes],
        }

    # ---------- health check ----------
    def health(self, playlist_name):
        p = self.find_by_name(playlist_name)
        if not p:
            return {"error": "playlist not found"}
        tracks = self.sp.get_playlist_tracks(p["id"])
        if not tracks:
            return {"playlist": playlist_name, "total": 0, "score": 0}

        # duplicates
        seen_keys = set()
        dupes = []
        for t in tracks:
            key = (t["name"].lower().strip(), t["artist"].lower().strip())
            if key in seen_keys:
                dupes.append(t)
            else:
                seen_keys.add(key)

        # features
        features = self.sp.batch_audio_features([t["id"] for t in tracks])
        valid = [(t, f) for t, f in zip(tracks, features) if f]
        target = self.get_profile(playlist_name)

        # outliers vs target
        outliers = []
        if target and valid:
            tsig = vibe_signature(target)
            for t, f in valid:
                d = vibe_distance(tsig, vibe_signature(f))
                if d > 0.45:
                    outliers.append({
                        "id": t["id"],
                        "name": t["name"],
                        "artist": t["artist"],
                        "distance": round(d, 3),
                    })

        # flow score
        if valid:
            flow_input = [{"id": t["id"], **f} for t, f in valid]
            flow_score = self.flow.flow_score(flow_input)
        else:
            flow_score = 0.5

        # overall
        dupe_penalty = min(0.3, len(dupes) * 0.05)
        outlier_penalty = min(0.3, len(outliers) * 0.03)
        score = max(0.0, min(1.0, flow_score - dupe_penalty - outlier_penalty))

        return {
            "playlist": playlist_name,
            "playlist_id": p["id"],
            "total": len(tracks),
            "duplicates": len(dupes),
            "duplicate_tracks": [{"name": t["name"], "artist": t["artist"]} for t in dupes[:20]],
            "outliers": outliers[:20],
            "outlier_count": len(outliers),
            "flow_score": round(flow_score, 3),
            "features_analyzed": len(valid),
            "score": round(score, 3),
        }

    def preview_playlist(self, playlist_name):
        p = self.find_by_name(playlist_name)
        if not p:
            return {"error": "playlist not found"}
        tracks = self.sp.get_playlist_tracks(p["id"])
        return {"playlist": playlist_name, "playlist_id": p["id"], "tracks": tracks}
