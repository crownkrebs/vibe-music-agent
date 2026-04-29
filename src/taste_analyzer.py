"""LLM-powered taste analyzer.

Runs once on first launch (or any time the user wants a refresh). Pulls the
user's full Spotify listening footprint, aggregates it into a stat block,
asks the configured LLM to translate the numbers into a *cultural* portrait,
and writes two artifacts to the project root:

    taste_profile.json   — structured (cultural worlds, top artists by world,
                           explicit ratio, current direction, negative space,
                           taste algorithm). Schema mirrors
                           reference/taste_profile.json so the rest of the app
                           keeps working.

    TASTE_PROFILE.md     — long-form prose. Second person ("You are someone
                           who..."). Identifies *worlds*, not genres. Names
                           specific identity-marker songs. Surfaces the
                           negative space.

Both files are gitignored. Templates committed under
`taste_profile.example.json` and `TASTE_PROFILE.example.md`.

LLM call pattern matches src/brain.py: Anthropic preferred, OpenAI fallback,
same model ladder, JSON-first parsing.
"""
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PROFILE_JSON = ROOT / "taste_profile.json"
PROFILE_MD = ROOT / "TASTE_PROFILE.md"
EXAMPLE_JSON = ROOT / "taste_profile.example.json"
EXAMPLE_MD = ROOT / "TASTE_PROFILE.example.md"


# ----------------------------------------------------------------------------
# Prompt
# ----------------------------------------------------------------------------

ANALYZER_SYSTEM_PROMPT = """You are a music taste anthropologist. You will be given a stat block describing one anonymous person's Spotify listening footprint — top artists across time ranges, full liked-songs history, language hints, explicit ratio, popularity distribution, and recency clusters.

Your job is to look past the genres and write a portrait of *who this person is musically* — the cultural worlds they live in, the moments their music serves, the negative space (what they conspicuously don't listen to), and the direction they're currently drifting.

You do not know this person's name, age, country, or anything biographical. Do NOT invent biography. Refer to them as "you" in the prose. If a cultural read is uncertain, hedge ("this reads like...") rather than asserting a city or origin.

THINGS YOU MUST DO

1. Identify cultural WORLDS, not genres. A world is a scene/context/identity (e.g. "the Mediterranean bridge", "late-night driving rap", "family-archive chanson", "internet-discovered city pop"). Two artists can sit in the same genre and belong to different worlds.

2. Name specific identity-marker SONGS, not just artists. Identity-markers are tracks the user clearly listens to repeatedly or has tagged old (high replay weight, listed in liked songs years ago, or sentimentally distinct from the rest of the library).

3. Describe taste as scenes/moments — "this is the soundtrack to X" — not as categories.

4. Surface NEGATIVE SPACE. What does this person noticeably NOT listen to (zero or near-zero histograms in obvious genres)? What does that absence reveal about how they relate to music?

5. Identify CURRENT DIRECTION. Use the recency cluster. Is this person maturing, regressing, opening up, narrowing? Be specific about which artists are signaling the shift.

6. Write a TASTE ALGORITHM — 5-7 bullet rules describing how this person actually picks music (e.g. "moment first, genre never", "voice texture matters more than beat", "discovery through travel, not algorithms"). Make these falsifiable observations grounded in the data, not platitudes.

OUTPUT FORMAT — return EXACTLY ONE valid JSON object, no prose, no markdown fences:

{
  "json_profile": {
    "languages": ["..."],
    "total_liked_songs": 0,
    "explicit_ratio": 0.0,
    "cultural_worlds": {
      "<world_key>": {
        "name": "Human-readable world name",
        "percentage": 0,
        "song_count": 0,
        "core_artists": ["Artist (count)", "..."],
        "explicit_ratio": 0.0,
        "description": "What this world means to this person."
      }
    },
    "current_direction": {
      "period": "e.g. last 4 weeks",
      "top_artists": ["Artist (count)", "..."],
      "signal": "What's shifting and why."
    },
    "popularity_distribution": {
      "underground_0_20": "0%",
      "niche_21_40": "0%",
      "mid_41_60": "0%",
      "popular_61_80": "0%",
      "mainstream_81_100": "0%"
    },
    "negative_space": {
      "zero": ["..."],
      "near_zero": ["..."],
      "principle": "What the absence reveals."
    },
    "taste_algorithm": [
      "Rule 1 — grounded in the data.",
      "Rule 2 — ..."
    ],
    "identity_marker_songs": [
      "Artist — Song (why it's an identity marker)"
    ]
  },
  "markdown_profile": "# WHO YOU ARE — MUSICALLY\\n\\nThis is not a genre breakdown. This is understanding.\\n\\n---\\n\\n## THE MUSICAL IDENTITY (not genres — worlds)\\n\\nYou don't listen to genres. You live in overlapping cultural worlds...\\n\\n### World 1: <Name> — <X>% of your library\\n<Specific artist list with counts. What this world MEANS in your daily life.>\\n\\n### World 2: ...\\n\\n---\\n\\n## WHAT YOU DO NOT LISTEN TO\\n<Negative space. What the absence reveals.>\\n\\n---\\n\\n## THE TASTE ALGORITHM (how you actually pick music)\\n1. ...\\n\\n---\\n\\n## CURRENT DIRECTION (<period>)\\n<Specific artists driving the shift.>\\n\\n---\\n\\n## WHAT THE AI MUST UNDERSTAND\\n1. ...\\n"
}

The markdown_profile must be a single string with \\n line breaks (it'll be written to disk as TASTE_PROFILE.md). Match the heading structure shown above. Use second person throughout. Be specific and confident on the data; hedge on cultural reads.

DO NOT copy the example prose verbatim. Generate fresh writing grounded in this specific user's stats. The output must be different for each user."""


# ----------------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------------

def _safe_artists(track):
    return [a.get("name") for a in (track.get("artists") or []) if a and a.get("name")]


def _added_at_to_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        try:
            # ISO 8601 with timezone
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None


def _bucket_popularity(pop):
    if pop is None:
        return None
    if pop <= 20:
        return "underground_0_20"
    if pop <= 40:
        return "niche_21_40"
    if pop <= 60:
        return "mid_41_60"
    if pop <= 80:
        return "popular_61_80"
    return "mainstream_81_100"


def _percent(n, total):
    if not total:
        return "0%"
    return f"{round(100.0 * n / total)}%"


def _detect_languages_from_genres(genres):
    """Best-effort language hint from artist genres. Spotify genre tags often
    encode language: 'french hip hop', 'arabic pop', 'k-pop', 'latin', etc."""
    hints = Counter()
    table = [
        ("french", "French"),
        ("francais", "French"),
        ("francoph", "French"),
        ("arab", "Arabic"),
        ("rai", "Arabic"),
        ("egyptian", "Arabic"),
        ("levantine", "Arabic"),
        ("k-pop", "Korean"),
        ("korean", "Korean"),
        ("j-pop", "Japanese"),
        ("japanese", "Japanese"),
        ("city pop", "Japanese"),
        ("latin", "Spanish"),
        ("reggaeton", "Spanish"),
        ("flamenco", "Spanish"),
        ("spanish", "Spanish"),
        ("portuguese", "Portuguese"),
        ("brasil", "Portuguese"),
        ("german", "German"),
        ("deutsch", "German"),
        ("italian", "Italian"),
        ("turkish", "Turkish"),
        ("afrobeat", "English/Afro"),
    ]
    for g in genres:
        gl = (g or "").lower()
        for needle, lang in table:
            if needle in gl:
                hints[lang] += 1
                break
        else:
            # default — if it's hip hop / pop / rock with no language token,
            # treat as English
            if any(k in gl for k in ("hip hop", "pop", "rock", "indie", "trap", "r&b", "soul")):
                hints["English"] += 1
    return hints


def _aggregate(spotify):
    """Pull everything we need from Spotify and aggregate into a stat block.

    Returns a dict that's safe to pass to an LLM (no oversized blobs, all
    numbers + a handful of names)."""
    out = {"fetched_at": datetime.now(timezone.utc).isoformat()}

    # ---- liked songs (full history) ----
    liked = []
    try:
        liked = spotify.get_all_liked_songs()
    except Exception as e:
        out["liked_error"] = str(e)
        liked = []

    artist_counts = Counter()
    artist_ids_seen = {}
    explicit_count = 0
    pop_buckets = Counter()
    monthly_adds = Counter()
    last_30d_artists = Counter()

    now = datetime.now(timezone.utc)
    cutoff_recent = now.timestamp() - 60 * 60 * 24 * 60  # last 60 days

    songs_by_artist_examples = defaultdict(list)

    for item in liked:
        track = item.get("track") or {}
        if not track.get("id"):
            continue
        names = _safe_artists(track)
        for n in names:
            artist_counts[n] += 1
        for a in track.get("artists") or []:
            if a.get("id") and a.get("name") and a["name"] not in artist_ids_seen:
                artist_ids_seen[a["name"]] = a["id"]
        if track.get("explicit"):
            explicit_count += 1
        b = _bucket_popularity(track.get("popularity"))
        if b:
            pop_buckets[b] += 1
        added = _added_at_to_dt(item.get("added_at"))
        if added:
            monthly_adds[added.strftime("%Y-%m")] += 1
            if added.timestamp() >= cutoff_recent:
                for n in names:
                    last_30d_artists[n] += 1
        # capture a track example per artist for identity-marker hints
        primary = names[0] if names else None
        if primary and len(songs_by_artist_examples[primary]) < 3:
            songs_by_artist_examples[primary].append(track.get("name") or "")

    out["liked_total"] = len(liked)
    out["explicit_count"] = explicit_count
    out["explicit_ratio"] = round(explicit_count / max(1, len(liked)), 3)
    out["popularity_buckets"] = {
        k: _percent(pop_buckets.get(k, 0), len(liked))
        for k in ("underground_0_20", "niche_21_40", "mid_41_60",
                 "popular_61_80", "mainstream_81_100")
    }
    out["top_artists_alltime"] = [
        {"name": n, "count": c, "examples": songs_by_artist_examples.get(n, [])[:3]}
        for n, c in artist_counts.most_common(80)
    ]
    out["recent_60d_artists"] = [
        {"name": n, "count": c} for n, c in last_30d_artists.most_common(25)
    ]
    out["monthly_add_histogram"] = dict(sorted(monthly_adds.items())[-18:])

    # ---- top artists / tracks across time ranges ----
    time_ranges = ("short_term", "medium_term", "long_term")
    out["top_artists_spotify"] = {}
    out["top_tracks_spotify"] = {}
    for tr in time_ranges:
        try:
            ta = spotify.sp.current_user_top_artists(limit=30, time_range=tr) or {}
            out["top_artists_spotify"][tr] = [
                {"name": a.get("name"), "genres": a.get("genres") or [],
                 "popularity": a.get("popularity")}
                for a in ta.get("items", []) if a
            ]
            for a in ta.get("items", []):
                if a and a.get("id") and a.get("name"):
                    artist_ids_seen.setdefault(a["name"], a["id"])
        except Exception as e:
            out["top_artists_spotify"][tr] = []
            out.setdefault("top_artist_errors", {})[tr] = str(e)
        try:
            tt = spotify.sp.current_user_top_tracks(limit=30, time_range=tr) or {}
            out["top_tracks_spotify"][tr] = [
                {"name": t.get("name"),
                 "artist": (t.get("artists") or [{}])[0].get("name"),
                 "popularity": t.get("popularity")}
                for t in tt.get("items", []) if t
            ]
        except Exception as e:
            out["top_tracks_spotify"][tr] = []
            out.setdefault("top_track_errors", {})[tr] = str(e)

    # ---- recently played ----
    try:
        rp = spotify.sp.current_user_recently_played(limit=50) or {}
        out["recently_played"] = [
            {"name": (it.get("track") or {}).get("name"),
             "artist": ((it.get("track") or {}).get("artists") or [{}])[0].get("name")}
            for it in rp.get("items", [])
        ]
    except Exception as e:
        out["recently_played"] = []
        out["recently_played_error"] = str(e)

    # ---- genre histogram from top-N artists ----
    top_artist_names = [a["name"] for a in out["top_artists_alltime"][:60]]
    name_to_id = {}
    for n in top_artist_names:
        if n in artist_ids_seen:
            name_to_id[n] = artist_ids_seen[n]
            continue
        try:
            hits = spotify.search_artist(n, limit=1)
        except Exception:
            hits = []
        if hits and hits[0].get("id"):
            name_to_id[n] = hits[0]["id"]
        time.sleep(0.02)
    genre_weights = Counter()
    artist_genres_map = {}
    if name_to_id:
        try:
            id_to_genres = spotify.artist_genres_batch(list(name_to_id.values()))
        except Exception:
            id_to_genres = {}
        for name, aid in name_to_id.items():
            genres = id_to_genres.get(aid) or []
            artist_genres_map[name] = genres
            cnt = artist_counts.get(name, 0)
            for g in genres:
                g = (g or "").strip().lower()
                if g:
                    genre_weights[g] += cnt
    out["genre_histogram"] = dict(genre_weights.most_common(40))
    out["artist_genres_sample"] = {n: gs for n, gs in list(artist_genres_map.items())[:40]}

    # ---- language hints ----
    lang_hints = _detect_languages_from_genres(list(genre_weights.keys()))
    out["language_hints"] = dict(lang_hints.most_common())

    # ---- market hint (from /me) ----
    try:
        me = spotify.me() or {}
        out["market_hint"] = me.get("country") or me.get("market") or None
    except Exception:
        out["market_hint"] = None

    return out


# ----------------------------------------------------------------------------
# LLM call
# ----------------------------------------------------------------------------

ANTHROPIC_MODELS = (
    "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
    "claude-opus-4-5", "claude-sonnet-4-5", "claude-3-5-sonnet-latest",
    "claude-3-5-haiku-latest",
)
OPENAI_MODEL = "gpt-4o"


class TasteAnalyzerError(Exception):
    pass


class TasteAnalyzer:
    def __init__(self, spotify_client, config):
        """config is the same dict the rest of the app uses (anthropic_api_key,
        openai_api_key, ...)."""
        self.sp = spotify_client
        self.config = config or {}
        self.anthropic = self._init_anthropic()
        self.openai = self._init_openai()

    def _init_anthropic(self):
        key = self.config.get("anthropic_api_key")
        if not key:
            return None
        try:
            import anthropic
            return anthropic.Anthropic(api_key=key)
        except Exception:
            return None

    def _init_openai(self):
        key = self.config.get("openai_api_key")
        if not key:
            return None
        try:
            from openai import OpenAI
            return OpenAI(api_key=key)
        except Exception:
            return None

    # ---------- main ----------
    def analyze(self, progress_cb=None):
        if not (self.anthropic or self.openai):
            raise TasteAnalyzerError(
                "No AI keys configured. Add anthropic_api_key or openai_api_key in Settings.")

        if progress_cb:
            progress_cb("Pulling your Spotify listening history...")
        stats = _aggregate(self.sp)

        if not stats.get("liked_total") and not stats.get("top_artists_spotify", {}).get("medium_term"):
            raise TasteAnalyzerError(
                "Couldn't read enough listening data from Spotify "
                "(liked songs and top artists are both empty).")

        if progress_cb:
            progress_cb("Asking the model to read between the numbers...")
        result = self._call_llm(stats)

        if not isinstance(result, dict) or "json_profile" not in result or "markdown_profile" not in result:
            raise TasteAnalyzerError(
                "LLM returned malformed analysis (missing json_profile / markdown_profile).")

        json_profile = result["json_profile"]
        md_profile = result["markdown_profile"]

        if not isinstance(json_profile, dict):
            raise TasteAnalyzerError("json_profile is not an object.")
        if not isinstance(md_profile, str) or not md_profile.strip():
            raise TasteAnalyzerError("markdown_profile is empty.")

        # stamp the stats summary into the json profile so the structured
        # artifact is reproducible/auditable.
        json_profile.setdefault("total_liked_songs", stats.get("liked_total", 0))
        json_profile.setdefault("explicit_ratio", stats.get("explicit_ratio", 0))
        json_profile.setdefault("popularity_distribution", stats.get("popularity_buckets", {}))
        json_profile["_meta"] = {
            "generated_at": stats.get("fetched_at"),
            "liked_songs_analyzed": stats.get("liked_total", 0),
            "model_provider": "anthropic" if self.anthropic else "openai",
        }

        if progress_cb:
            progress_cb("Writing TASTE_PROFILE.md and taste_profile.json...")
        PROFILE_JSON.write_text(
            json.dumps(json_profile, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        PROFILE_MD.write_text(md_profile, encoding="utf-8")

        return {
            "ok": True,
            "liked_songs_analyzed": stats.get("liked_total", 0),
            "json_path": str(PROFILE_JSON),
            "md_path": str(PROFILE_MD),
            "worlds": list((json_profile.get("cultural_worlds") or {}).keys()),
        }

    # ---------- LLM ----------
    def _build_user_message(self, stats):
        # Compact the stat block — strip noisy fields, keep the signal.
        compact = {
            "liked_total": stats.get("liked_total"),
            "explicit_ratio": stats.get("explicit_ratio"),
            "popularity_buckets": stats.get("popularity_buckets"),
            "top_artists_alltime": stats.get("top_artists_alltime", [])[:50],
            "recent_60d_artists": stats.get("recent_60d_artists", []),
            "monthly_add_histogram": stats.get("monthly_add_histogram", {}),
            "top_artists_spotify": {
                k: [a["name"] for a in v[:20] if a.get("name")]
                for k, v in (stats.get("top_artists_spotify") or {}).items()
            },
            "top_tracks_spotify": {
                k: [f"{t.get('artist','')} — {t.get('name','')}" for t in v[:20]]
                for k, v in (stats.get("top_tracks_spotify") or {}).items()
            },
            "recently_played": [
                f"{r.get('artist','')} — {r.get('name','')}"
                for r in (stats.get("recently_played") or [])[:30]
            ],
            "genre_histogram": stats.get("genre_histogram"),
            "language_hints": stats.get("language_hints"),
            "market_hint": stats.get("market_hint"),
        }
        return (
            "Here is the user's listening footprint. Read it, then produce the "
            "JSON object specified in the system prompt — both the structured "
            "json_profile AND the markdown_profile, in one response.\n\n"
            f"```json\n{json.dumps(compact, ensure_ascii=False, indent=2)}\n```"
        )

    def _call_llm(self, stats):
        user_msg = self._build_user_message(stats)
        errors = []
        raw = None
        if self.anthropic:
            raw, err = self._call_anthropic(user_msg)
            if err:
                errors.append(f"Anthropic: {err}")
        if raw is None and self.openai:
            raw, err = self._call_openai(user_msg)
            if err:
                errors.append(f"OpenAI: {err}")
        if raw is None:
            raise TasteAnalyzerError(
                "AI offline — " + ("; ".join(errors) if errors else "no AI keys configured"))
        return self._parse_json(raw)

    def _call_anthropic(self, user_msg):
        last_err = None
        for model in ANTHROPIC_MODELS:
            try:
                resp = self.anthropic.messages.create(
                    model=model,
                    max_tokens=8000,
                    system=ANALYZER_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_msg}],
                )
                return resp.content[0].text, None
            except Exception as e:
                last_err = f"{model}: {type(e).__name__}: {str(e)[:180]}"
                continue
        return None, last_err

    def _call_openai(self, user_msg):
        try:
            resp = self.openai.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=8000,
                messages=[
                    {"role": "system", "content": ANALYZER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
            )
            return resp.choices[0].message.content, None
        except Exception as e:
            return None, f"{type(e).__name__}: {str(e)[:180]}"

    def _parse_json(self, raw):
        text = (raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except Exception:
            pass
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
        return None
