"""Recommendation pipeline. All tracks come from Spotify's API — zero hallucination.

Two pipelines:
  - recommend()                  : legacy param-based path (seed_tracks + audio ranges).
  - recommend_from_candidates()  : new Option-C path. LLM proposes {artist,title} pairs;
                                   we search Spotify, verify by token-match, filter by
                                   audio_target, pad with library neighbors.
"""
import random
import re
import unicodedata
from .vibe import percentile


def _norm(s):
    """Normalize a string for fuzzy comparison: lowercase, strip accents, alnum only."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokens(s):
    return [t for t in _norm(s).split() if t]


_FEAT_WORDS = {"feat", "ft", "featuring", "with", "prod", "remix", "version",
               "remastered", "remaster", "edit", "mono", "stereo", "live",
               "acoustic", "demo", "bonus", "deluxe", "explicit"}


def _title_match(target, got):
    t_toks = [w for w in _tokens(target) if w not in _FEAT_WORDS]
    g_toks = [w for w in _tokens(got) if w not in _FEAT_WORDS]
    if not t_toks or not g_toks:
        return False
    g_set = set(g_toks)
    overlap = sum(1 for w in t_toks if w in g_set)
    # accept if ≥60% of target title words are present, or target is a clean substring
    if overlap / len(t_toks) >= 0.6:
        return True
    if _norm(target) in _norm(got):
        return True
    return False


def _artist_match(target, result_artists):
    tn = _norm(target)
    if not tn:
        return False
    for a in result_artists or []:
        an = _norm(a)
        if not an:
            continue
        if tn == an or tn in an or an in tn:
            return True
        # token overlap fallback
        t_toks = set(_tokens(target))
        a_toks = set(_tokens(a))
        if t_toks and a_toks and len(t_toks & a_toks) / max(1, len(t_toks)) >= 0.7:
            return True
    return False


def _features_in_target(feat, audio_target):
    """LEGACY — kept so `recommend()` / `_fallback_recommend()` (legacy paths,
    not the active pipeline) still import-compatible. The new pipeline uses
    `_metadata_fit` below. audio_features is 403-dead at the API for this app."""
    if not feat or not audio_target:
        return True
    slack = 0.08
    for key in ("energy", "valence", "danceability", "acousticness",
                "instrumentalness", "speechiness"):
        rng = audio_target.get(key)
        if not (isinstance(rng, (list, tuple)) and len(rng) == 2):
            continue
        v = feat.get(key)
        if v is None:
            continue
        if v < rng[0] - slack or v > rng[1] + slack:
            return False
    tempo_rng = audio_target.get("tempo")
    if isinstance(tempo_rng, (list, tuple)) and len(tempo_rng) == 2:
        t = feat.get("tempo")
        if t is not None:
            for candidate in (t, t / 2, t * 2):
                if tempo_rng[0] - 8 <= candidate <= tempo_rng[1] + 8:
                    break
            else:
                return False
    return True


# ------------ NEW metadata-fit gate (replaces audio-feature filtering) -----

def _era_from_release_date(date_str):
    """Parse a release_date string ('1994', '1994-05', '1994-05-03') → year int."""
    if not date_str:
        return None
    s = str(date_str).strip()
    if not s:
        return None
    try:
        return int(s.split("-")[0])
    except Exception:
        return None


# Vibe-hint patterns → era windows. Only trigger when the vibe hint names an era.
_ERA_PATTERNS = [
    (re.compile(r"\b(19)?60s\b|sixties", re.I), (1960, 1969)),
    (re.compile(r"\b(19)?70s\b|seventies", re.I), (1970, 1979)),
    (re.compile(r"\b(19)?80s\b|eighties", re.I), (1980, 1989)),
    (re.compile(r"\b(19)?90s\b|nineties", re.I), (1990, 1999)),
    (re.compile(r"\b(20)?00s\b|aughts|y2k", re.I), (2000, 2009)),
    (re.compile(r"\b(20)?10s\b", re.I), (2010, 2019)),
    (re.compile(r"\b(20)?20s\b|recent|new|current", re.I), (2020, 2099)),
    (re.compile(r"\bclassic|vintage|old[- ]?school\b", re.I), (1960, 1999)),
]


def _era_window(vibe_hint):
    """Pull (lo, hi) year window from vibe text, or None if no era hint."""
    if not vibe_hint:
        return None
    s = str(vibe_hint)
    for pat, window in _ERA_PATTERNS:
        if pat.search(s):
            return window
    return None


def _era_fit(year, window):
    """1.0 inside window, soft falloff outside, 1.0 if no window."""
    if window is None:
        return 1.0
    if year is None:
        return 0.5  # unknown year, don't punish
    lo, hi = window
    if lo <= year <= hi:
        return 1.0
    # linear falloff: 5 years out of window = 0.6, 10 years = 0.2, 20+ = 0
    dist = min(abs(year - lo), abs(year - hi))
    return max(0.0, 1.0 - dist / 20.0)


def _popularity_fit(pop):
    """Prefer mid-range 30-70. Ultra-obscure (<10) and mainstream-glut (>85) dip."""
    if pop is None:
        return 0.6
    try:
        p = float(pop)
    except Exception:
        return 0.6
    if 30 <= p <= 70:
        return 1.0
    if p < 30:
        # 0 → 0.35, 10 → 0.55, 30 → 1.0
        return max(0.35, 0.35 + (p / 30.0) * 0.65)
    # p > 70
    if p <= 85:
        # 70→1.0, 85→0.75
        return 1.0 - (p - 70) * (0.25 / 15.0)
    # p > 85 → steeper drop but never below 0.4
    return max(0.4, 0.75 - (p - 85) * (0.35 / 15.0))


def _metadata_fit(track, artist_genres, taste, vibe_hint=None):
    """Score a verified Spotify track on metadata signals only (no audio features).

    Returns dict with per-component scores + combined 'fit'.
    Weights:
      genre_affinity     0.45
      era_fit            0.15
      popularity_fit     0.15
      artist_familiarity 0.25 (tiebreaker, not a gate — new artists are allowed)
    """
    artist_name = ""
    if track.get("artists"):
        artist_name = track["artists"][0].get("name") or ""

    g_aff = taste.genre_affinity(artist_genres) if artist_genres else (
        # no artist genres returned — neutral rather than punishing (many
        # non-Anglo artists have empty genre lists on Spotify)
        0.5
    )
    year = _era_from_release_date(
        (track.get("album") or {}).get("release_date")
    )
    window = _era_window(vibe_hint)
    e_fit = _era_fit(year, window)
    p_fit = _popularity_fit(track.get("popularity"))
    fam = taste.artist_familiarity(artist_name)

    fit = (
        g_aff * 0.45 +
        e_fit * 0.15 +
        p_fit * 0.15 +
        fam * 0.25
    )
    return {
        "fit": round(max(0.0, min(1.0, fit)), 3),
        "genre_affinity": round(g_aff, 3),
        "era_fit": round(e_fit, 3),
        "popularity_fit": round(p_fit, 3),
        "familiarity": round(fam, 3),
        "year": year,
        "window": window,
    }


class Recommender:
    def __init__(self, spotify, taste, flow, learner, playlist_module):
        self.sp = spotify
        self.taste = taste
        self.flow = flow
        self.learner = learner
        self.playlists = playlist_module

    # ==========================================================
    # NEW PIPELINE — LLM candidates → search → verify → features
    # ==========================================================
    def recommend_from_candidates(
        self,
        candidates,
        audio_target=None,
        count=8,
        playlist_name=None,
        existing_ids=None,
        flow_style="smooth",
        search_queries=None,
        library_fallback=True,
        vibe_hint=None,
    ):
        """Given {artist,title} candidates from the LLM, verify + metadata-fit
        filter + pad. audio_target is accepted for backward compat but the
        feature-range filter is no longer applied (API 403-dead); era_fit is
        driven by `vibe_hint` (a string like '90s hip-hop').

        Returns {tracks: [...], verified: int, rejected: [...],
                 padded_from_library: int, padded_from_search: int,
                 scores: {track_id: metadata_fit_dict}}.
        """
        existing = set(existing_ids or [])
        target_count = max(1, int(count or 8))
        candidates = candidates or []
        search_queries = search_queries or []

        verified_tracks = []
        verified_ids = set()
        rejected = []
        # keep (artist, title) for each verified track → source candidate
        track_source = {}

        # ----- 1. verify each candidate via Spotify search -----
        for cand in candidates:
            artist = cand.get("artist", "").strip()
            title = cand.get("title", "").strip()
            if not (artist and title):
                rejected.append({"reason": "empty", "artist": artist, "title": title})
                continue
            hits = self.sp.search_artist_title(artist, title, limit=5)
            matched = None
            for h in hits:
                hid = h.get("id")
                if not hid or hid in existing or hid in verified_ids:
                    continue
                artists = [a["name"] for a in h.get("artists", []) if a.get("name")]
                if not _artist_match(artist, artists):
                    continue
                if not _title_match(title, h.get("name", "")):
                    continue
                matched = h
                break
            if matched:
                verified_tracks.append(matched)
                verified_ids.add(matched["id"])
                track_source[matched["id"]] = {"artist": artist, "title": title}
            else:
                rejected.append({
                    "reason": "no_match" if hits else "no_search_hit",
                    "artist": artist, "title": title,
                })

        # ----- 2. add extra search_queries hits (cultural fallback) -----
        for q in search_queries[:6]:
            for h in self.sp.search_track(str(q), limit=6):
                hid = h.get("id")
                if hid and hid not in existing and hid not in verified_ids:
                    verified_tracks.append(h)
                    verified_ids.add(hid)

        # ----- 3. batch-fetch artist genres for all verified candidates -----
        primary_artist_ids = []
        for t in verified_tracks:
            arts = t.get("artists") or []
            if arts and arts[0].get("id"):
                primary_artist_ids.append(arts[0]["id"])
        artist_genre_map = {}
        if primary_artist_ids:
            try:
                artist_genre_map = self.sp.artist_genres_batch(primary_artist_ids)
            except Exception:
                artist_genre_map = {}

        # ----- 4. metadata_fit score each, drop below threshold -----
        FIT_THRESHOLD = 0.35
        scores = {}
        kept = []
        for t in verified_tracks:
            arts = t.get("artists") or []
            aid = arts[0]["id"] if (arts and arts[0].get("id")) else None
            genres = artist_genre_map.get(aid, []) if aid else []
            m = _metadata_fit(t, genres, self.taste, vibe_hint=vibe_hint)
            m["genres"] = genres
            scores[t["id"]] = m
            if m["fit"] < FIT_THRESHOLD:
                # silent rejection, reason = genre_mismatch when genre is the
                # dominant pull-down, else metadata_fit_low
                reason = "genre_mismatch" if m["genre_affinity"] < 0.2 else "metadata_fit_low"
                rejected.append({
                    "reason": reason,
                    "artist": arts[0]["name"] if arts else "",
                    "title": t.get("name", ""),
                    "fit": m["fit"],
                    "genre_affinity": m["genre_affinity"],
                })
                continue
            kept.append(t)

        # safety net: if the gate was too aggressive, relax once
        if len(kept) < min(target_count, max(3, len(verified_tracks) // 2)) and verified_tracks:
            # readmit everything verified, but keep scores so ranking still
            # reflects true fit — the threshold just gets waived.
            kept = list(verified_tracks)

        padded_from_library = 0
        padded_from_search = 0
        # ----- 5. pad from library neighbors if short -----
        if len(kept) < target_count and library_fallback:
            need = target_count - len(kept)
            extras = self._pad_from_library(
                need=need * 2,
                audio_target=audio_target or {},
                exclude=existing | verified_ids,
                seed_artists=[c.get("artist") for c in candidates if c.get("artist")],
            )
            # score padded tracks too so they can rank against candidates
            extra_aids = []
            for t in extras:
                arts = t.get("artists") or []
                if arts and arts[0].get("id"):
                    extra_aids.append(arts[0]["id"])
            pad_genre_map = {}
            if extra_aids:
                try:
                    pad_genre_map = self.sp.artist_genres_batch(extra_aids)
                except Exception:
                    pad_genre_map = {}
            for t in extras:
                if len(kept) >= target_count:
                    break
                tid = t.get("id")
                if not tid or tid in verified_ids:
                    continue
                arts = t.get("artists") or []
                aid = arts[0]["id"] if (arts and arts[0].get("id")) else None
                genres = pad_genre_map.get(aid, []) if aid else []
                m = _metadata_fit(t, genres, self.taste, vibe_hint=vibe_hint)
                m["genres"] = genres
                scores[tid] = m
                kept.append(t)
                verified_ids.add(tid)
                padded_from_library += 1

        # ----- 6. rank by fit, slice to count -----
        def _rank_key(t):
            return scores.get(t["id"], {}).get("fit", 0.0)
        kept.sort(key=_rank_key, reverse=True)

        # ----- 7. pack -----
        profile = self.playlists.get_profile(playlist_name) if playlist_name else None
        packed = []
        for t in kept[:target_count]:
            m = scores.get(t["id"], {})
            artist_name = t["artists"][0]["name"] if t.get("artists") else ""
            packed.append({
                "id": t["id"],
                "name": t.get("name", ""),
                "artist": artist_name,
                "artists": [a["name"] for a in t.get("artists", [])],
                "album": t.get("album", {}).get("name", ""),
                "album_art": (t["album"]["images"][0]["url"]
                              if t.get("album", {}).get("images") else None),
                "genres": m.get("genres", []),
                "features": None,  # audio_features API is dead; left null for transparency
                "familiarity": m.get("familiarity", 0.0),
                "score": m.get("fit", 0.0),
                "genre_affinity": m.get("genre_affinity", 0.0),
                "era_fit": m.get("era_fit", 1.0),
                "popularity_fit": m.get("popularity_fit", 0.6),
                "year": m.get("year"),
                "preview_url": t.get("preview_url"),
                "popularity": t.get("popularity", 0),
                "explicit": t.get("explicit", False),
            })

        # ----- 8. flow-order (best-effort; works without features via Camelot/fallback) -----
        if len(packed) > 2:
            flow_input = [{"id": c["id"]} for c in packed]
            try:
                ordered = self.flow.order(flow_input, style=flow_style)
                id_map = {c["id"]: c for c in packed}
                packed = [id_map[o["id"]] for o in ordered if o["id"] in id_map]
            except Exception:
                pass  # keep fit-sorted order if flow engine chokes without features

        return {
            "tracks": packed,
            "verified": len(verified_ids) - padded_from_library,
            "rejected": rejected[:30],
            "padded_from_library": padded_from_library,
            "padded_from_search": padded_from_search,
            "candidate_count": len(candidates),
            "scores": {tid: {k: v for k, v in m.items() if k != "window"}
                       for tid, m in scores.items()},
        }

    def _pad_from_library(self, need, audio_target, exclude, seed_artists):
        """Pad with tracks from the user's taste neighborhood when LLM candidates come up short."""
        if need <= 0:
            return []
        out = []
        seen = set(exclude)
        # anchor 1: top tracks of the LLM-named artists
        for artist_name in seed_artists[:5]:
            if len(out) >= need:
                break
            for art in self.sp.search_artist(artist_name, limit=1):
                aid = art.get("id")
                if not aid:
                    continue
                for t in self.sp.artist_top_tracks(aid):
                    tid = t.get("id")
                    if tid and tid not in seen:
                        seen.add(tid)
                        out.append(t)
                        if len(out) >= need:
                            break
        # anchor 2: top tracks of the user's own top artists
        if len(out) < need:
            for artist_name, _cnt in self.taste.top_artists(30):
                if len(out) >= need:
                    break
                for art in self.sp.search_artist(artist_name, limit=1):
                    aid = art.get("id")
                    if not aid:
                        continue
                    for t in self.sp.artist_top_tracks(aid):
                        tid = t.get("id")
                        if tid and tid not in seen:
                            seen.add(tid)
                            out.append(t)
                            if len(out) >= need:
                                break
        return out

    def recommend(self, params, existing_ids=None):
        """Generate verified recommendations.
        params: dict with ranges (energy, valence, danceability, tempo, acousticness)
                plus genres, seed_from, flow_style, count, discovery_pct, playlist.
        existing_ids: track IDs already in target playlist / to exclude.
        """
        existing = set(existing_ids or [])
        target_count = int(params.get("count", 12))
        playlist_name = params.get("playlist", "")
        flow_style = params.get("flow_style", "smooth")
        discovery = float(params.get("discovery_pct", 0.25))

        # search-query mode: for culturally specific content outside Spotify's genre taxonomy
        search_queries = params.get("search_queries") or []
        if search_queries:
            return self._recommend_via_search(search_queries, params, existing, target_count, flow_style)

        # 1. seeds
        seeds = self._get_seeds(params)
        seed_genres = self._sanitize_genres(params.get("genres") or [])

        # 2. audio range params
        api_params = {}
        for feat in ("energy", "valence", "danceability", "acousticness",
                     "instrumentalness", "speechiness"):
            rng = params.get(feat)
            if isinstance(rng, (list, tuple)) and len(rng) == 2:
                api_params[f"min_{feat}"] = max(0.0, float(rng[0]))
                api_params[f"max_{feat}"] = min(1.0, float(rng[1]))
                api_params[f"target_{feat}"] = (api_params[f"min_{feat}"] + api_params[f"max_{feat}"]) / 2
        tempo = params.get("tempo")
        if isinstance(tempo, (list, tuple)) and len(tempo) == 2:
            api_params["min_tempo"] = float(tempo[0])
            api_params["max_tempo"] = float(tempo[1])
            api_params["target_tempo"] = (float(tempo[0]) + float(tempo[1])) / 2

        # 3. over-fetch for filtering
        overfetch = max(30, target_count * 4)
        try:
            recs = self.sp.get_recommendations(
                seed_tracks=seeds[:3],
                seed_genres=seed_genres[:2] if seed_genres else None,
                limit=min(100, overfetch),
                **api_params,
            )
        except Exception as e:
            # fall back to broader search using seed artists from taste
            return self._fallback_recommend(params, existing, target_count, flow_style)

        tracks = recs.get("tracks", []) if recs else []
        if not tracks:
            return self._fallback_recommend(params, existing, target_count, flow_style)

        # dedupe vs existing + duplicate titles
        seen_ids, seen_keys = set(), set()
        dedup = []
        for t in tracks:
            tid = t.get("id")
            if not tid or tid in existing or tid in seen_ids:
                continue
            key = (t.get("name", "").lower(), t["artists"][0]["name"].lower() if t.get("artists") else "")
            if key in seen_keys:
                continue
            seen_ids.add(tid)
            seen_keys.add(key)
            dedup.append(t)
        tracks = dedup

        if not tracks:
            return []

        # 4. audio features
        features = self.sp.batch_audio_features([t["id"] for t in tracks])

        # 5. playlist profile for scoring
        profile = self.playlists.get_profile(playlist_name)

        # 6. score
        scored = []
        for t, feat in zip(tracks, features):
            if not feat:
                feat = self._synth_features(t, params)
            artist = t["artists"][0]["name"] if t.get("artists") else ""
            taste_score = self.taste.score(feat)
            playlist_score = self.taste.score(feat, profile) if profile else taste_score
            familiarity = self.taste.artist_familiarity(artist)
            # discovery balance: keep ~discovery % unfamiliar
            combined = (taste_score * 0.30) + (playlist_score * 0.55) + (familiarity * 0.15)

            scored.append({
                "id": t["id"],
                "name": t["name"],
                "artist": artist,
                "artists": [a["name"] for a in t.get("artists", [])],
                "album": t.get("album", {}).get("name", ""),
                "album_art": (t["album"]["images"][0]["url"]
                              if t.get("album", {}).get("images") else None),
                "features": feat,
                "familiarity": familiarity,
                "score": round(combined, 3),
                "taste_score": round(taste_score, 3),
                "playlist_score": round(playlist_score, 3),
                "preview_url": t.get("preview_url"),
                "popularity": t.get("popularity", 0),
                "explicit": t.get("explicit", False),
            })

        scored.sort(key=lambda x: x["score"], reverse=True)

        # 7. enforce discovery balance
        familiar = [s for s in scored if s["familiarity"] > 0.15]
        unfamiliar = [s for s in scored if s["familiarity"] <= 0.15]
        fam_needed = int(target_count * (1 - discovery))
        unf_needed = target_count - fam_needed
        chosen = familiar[:fam_needed] + unfamiliar[:unf_needed]
        if len(chosen) < target_count:
            chosen += [s for s in scored if s not in chosen][:target_count - len(chosen)]
        chosen = chosen[:target_count]

        # 8. flow ordering
        if len(chosen) > 2:
            flow_input = [{"id": c["id"], **(c["features"] or {})} for c in chosen]
            ordered = self.flow.order(flow_input, style=flow_style)
            id_map = {c["id"]: c for c in chosen}
            chosen = [id_map[o["id"]] for o in ordered if o["id"] in id_map]

        return chosen

    def _recommend_via_search(self, queries, params, existing, target_count, flow_style):
        """Use Spotify search for culturally specific content (regional folk, vintage subgenres, etc.)."""
        candidates = {}
        per_query = max(6, (target_count * 3) // max(1, len(queries)))
        for q in queries:
            for t in self.sp.search_track(str(q), limit=per_query):
                tid = t.get("id")
                if not tid or tid in existing or tid in candidates:
                    continue
                candidates[tid] = t
        if not candidates:
            return []
        tracks = list(candidates.values())
        features = self.sp.batch_audio_features([t["id"] for t in tracks])
        profile = self.playlists.get_profile(params.get("playlist", ""))
        scored = []
        for t, feat in zip(tracks, features):
            feat = feat or self._synth_features(t, params)
            score = self.taste.score(feat, profile) if profile else self.taste.score(feat)
            scored.append({
                "id": t["id"],
                "name": t["name"],
                "artist": t["artists"][0]["name"] if t.get("artists") else "",
                "artists": [a["name"] for a in t.get("artists", [])],
                "album": t.get("album", {}).get("name", ""),
                "album_art": (t["album"]["images"][0]["url"]
                              if t.get("album", {}).get("images") else None),
                "features": feat,
                "familiarity": self.taste.artist_familiarity(
                    t["artists"][0]["name"] if t.get("artists") else ""),
                "score": round(score, 3),
                "taste_score": round(score, 3),
                "playlist_score": round(score, 3),
                "preview_url": t.get("preview_url"),
                "popularity": t.get("popularity", 0),
                "explicit": t.get("explicit", False),
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        chosen = scored[:target_count]
        if len(chosen) > 2:
            flow_input = [{"id": c["id"], **(c["features"] or {})} for c in chosen]
            ordered = self.flow.order(flow_input, style=flow_style)
            id_map = {c["id"]: c for c in chosen}
            chosen = [id_map[o["id"]] for o in ordered if o["id"] in id_map]
        return chosen

    def _fallback_recommend(self, params, existing, target_count, flow_style):
        """Plan B: search by artist+genre, no recommendations endpoint."""
        top_artists = [a for a, _ in self.taste.top_artists(15)]
        candidates = {}
        for artist in top_artists[:5]:
            for t in self.sp.search_track(f"artist:{artist}", limit=10):
                tid = t.get("id")
                if tid and tid not in existing and tid not in candidates:
                    candidates[tid] = t
        if not candidates:
            return []
        tracks = list(candidates.values())[:target_count * 3]
        features = self.sp.batch_audio_features([t["id"] for t in tracks])
        profile = self.playlists.get_profile(params.get("playlist", ""))
        scored = []
        for t, feat in zip(tracks, features):
            if not feat:
                continue
            score = self.taste.score(feat, profile)
            scored.append({
                "id": t["id"],
                "name": t["name"],
                "artist": t["artists"][0]["name"] if t.get("artists") else "",
                "artists": [a["name"] for a in t.get("artists", [])],
                "album": t.get("album", {}).get("name", ""),
                "album_art": (t["album"]["images"][0]["url"]
                              if t.get("album", {}).get("images") else None),
                "features": feat,
                "familiarity": 0.8,
                "score": round(score, 3),
                "preview_url": t.get("preview_url"),
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:target_count]

    def _synth_features(self, track, params):
        """Approximate features from request params when API doesn't return them."""
        def mid(r):
            return (r[0] + r[1]) / 2 if isinstance(r, (list, tuple)) else r
        return {
            "energy": mid(params.get("energy", [0.5, 0.7])),
            "valence": mid(params.get("valence", [0.4, 0.7])),
            "danceability": mid(params.get("danceability", [0.5, 0.75])),
            "tempo": mid(params.get("tempo", [100, 130])),
            "acousticness": mid(params.get("acousticness", [0.05, 0.4])),
        }

    def _get_seeds(self, params):
        seed_from = params.get("seed_from", "liked_songs")
        if isinstance(seed_from, list):
            return [s for s in seed_from if s][:5]
        if seed_from == "liked_songs" or not seed_from:
            ids = self.sp.recent_liked_ids(40)
            return self._pick_diverse(ids, 3)
        # else — treat as playlist id or name
        try:
            tracks = self.sp.get_playlist_tracks(seed_from)
            ids = [t["id"] for t in tracks][:30]
            return self._pick_diverse(ids, 3)
        except Exception:
            ids = self.sp.recent_liked_ids(40)
            return self._pick_diverse(ids, 3)

    def _pick_diverse(self, track_ids, count=3):
        if not track_ids:
            return []
        features = self.sp.batch_audio_features(track_ids[:50])
        paired = [(tid, f) for tid, f in zip(track_ids, features) if f]
        if not paired:
            return track_ids[:count]
        paired.sort(key=lambda x: x[1].get("energy") or 0.5)
        step = max(1, len(paired) // count)
        return [paired[i * step][0] for i in range(min(count, len(paired)))]

    _VALID_GENRES = {
        "acoustic", "afrobeats", "alt-rock", "alternative", "ambient", "anime", "blues",
        "bossa-nova", "brazil", "breakbeat", "british", "cantopop", "chill", "classical",
        "club", "country", "dance", "dancehall", "death-metal", "deep-house", "disco",
        "disney", "drum-and-bass", "dub", "dubstep", "edm", "electro", "electronic", "emo",
        "folk", "french", "funk", "garage", "german", "gospel", "goth", "grindcore",
        "groove", "grunge", "guitar", "happy", "hard-rock", "hardcore", "hardstyle",
        "heavy-metal", "hip-hop", "holidays", "honky-tonk", "house", "idm", "indian",
        "indie", "indie-pop", "industrial", "iranian", "j-dance", "j-idol", "j-pop",
        "j-rock", "jazz", "k-pop", "kids", "latin", "latino", "malay", "mandopop", "metal",
        "metalcore", "minimal-techno", "movies", "mpb", "new-age", "new-release", "opera",
        "pagode", "party", "philippines-opm", "piano", "pop", "pop-film", "post-dubstep",
        "power-pop", "progressive-house", "psych-rock", "punk", "punk-rock", "r-n-b",
        "rainy-day", "reggae", "reggaeton", "road-trip", "rock", "rock-n-roll",
        "rockabilly", "romance", "sad", "salsa", "samba", "sertanejo", "show-tunes",
        "singer-songwriter", "ska", "sleep", "songwriter", "soul", "soundtracks",
        "spanish", "study", "summer", "swedish", "synth-pop", "tango", "techno", "trance",
        "trip-hop", "turkish", "work-out", "world-music",
    }

    def _sanitize_genres(self, genres):
        out = []
        for g in genres:
            g_clean = str(g).lower().strip().replace(" ", "-").replace("&", "and")
            # common aliases
            aliases = {
                "french-rap": "french", "french-hip-hop": "hip-hop",
                "rnb": "r-n-b", "r&b": "r-n-b", "rhythm-and-blues": "r-n-b",
                "trap": "hip-hop", "drill": "hip-hop", "rap": "hip-hop",
                "arabic": "world-music", "afro": "afrobeats",
                "amapiano": "afrobeats", "bossa": "bossa-nova",
            }
            g_clean = aliases.get(g_clean, g_clean)
            if g_clean in self._VALID_GENRES and g_clean not in out:
                out.append(g_clean)
        return out

    def learn_from_reference(self, playlist_url):
        """Extract vibe params from a reference Spotify playlist URL."""
        pid = playlist_url.strip().rstrip("/").split("/")[-1].split("?")[0]
        try:
            tracks = self.sp.get_playlist_tracks(pid)
        except Exception:
            return {}
        if not tracks:
            return {}
        features = self.sp.batch_audio_features([t["id"] for t in tracks])
        valid = [f for f in features if f]
        if not valid:
            return {"seed_from": [t["id"] for t in tracks[:5]]}

        def rng(key, lo=15, hi=85):
            vals = [f.get(key) for f in valid if f.get(key) is not None]
            if not vals:
                return None
            return [round(percentile(vals, lo), 3), round(percentile(vals, hi), 3)]

        return {
            "energy": rng("energy"),
            "valence": rng("valence"),
            "danceability": rng("danceability"),
            "tempo": rng("tempo"),
            "acousticness": rng("acousticness"),
            "seed_from": [t["id"] for t in tracks[:5]],
        }
