"""Metadata-fit gate sanity tests. Run directly — no pytest required.

    py tests/test_match.py

Loads TasteDNA from the existing snapshot (does NOT rebuild), then pushes
5 vibes x (3 good + 3 deliberate bad-genre) candidates through
recommend_from_candidates and reports fit scores + rejections.

Expected: the deliberate bad-genre candidates should land in `rejected` with
reason "genre_mismatch" (low genre_affinity) in most cases.
"""
import json
import os
import sys

# Make `src` importable when run from repo root
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from src.db import DB
from src.spotify import SpotifyClient
from src.taste import TasteDNA
from src.flow import FlowEngine
from src import playlist as playlist_module
from src.recommend import Recommender


# 5 vibes × 6 candidates each. "good" = on-vibe artists that should plausibly
# match. "bad" = deliberate off-genre tracks (mainstream pop rock, kids
# music, opera where it doesn't belong, etc.) that should trip the gate.
TEST_CASES = [
    {
        "label": "trap",
        "vibe_hint": "modern trap, 808s, atlanta",
        "good": [
            {"artist": "Future",   "title": "Mask Off"},
            {"artist": "Travis Scott", "title": "Sicko Mode"},
            {"artist": "21 Savage", "title": "A Lot"},
        ],
        "bad": [
            {"artist": "The Beatles", "title": "Hey Jude"},
            {"artist": "Andrea Bocelli", "title": "Con Te Partirò"},
            {"artist": "Taylor Swift", "title": "Shake It Off"},
        ],
    },
    {
        "label": "acoustic chill",
        "vibe_hint": "acoustic singer-songwriter chill folk",
        "good": [
            {"artist": "Bon Iver", "title": "Skinny Love"},
            {"artist": "José González", "title": "Heartbeats"},
            {"artist": "Ben Howard", "title": "Only Love"},
        ],
        "bad": [
            {"artist": "Skrillex", "title": "Bangarang"},
            {"artist": "Slipknot", "title": "Duality"},
            {"artist": "Cardi B",  "title": "WAP"},
        ],
    },
    {
        "label": "90s r&b",
        "vibe_hint": "90s r&b slow jams",
        "good": [
            {"artist": "Aaliyah", "title": "Are You That Somebody"},
            {"artist": "TLC",     "title": "Waterfalls"},
            {"artist": "D'Angelo", "title": "Brown Sugar"},
        ],
        "bad": [
            {"artist": "Metallica", "title": "Enter Sandman"},
            {"artist": "Daft Punk", "title": "Around the World"},
            {"artist": "Ariana Grande", "title": "7 rings"},
        ],
    },
    {
        "label": "afrobeat",
        "vibe_hint": "afrobeats amapiano lagos",
        "good": [
            {"artist": "Burna Boy", "title": "Ye"},
            {"artist": "Wizkid",    "title": "Essence"},
            {"artist": "Rema",      "title": "Calm Down"},
        ],
        "bad": [
            {"artist": "Coldplay",  "title": "Yellow"},
            {"artist": "Johnny Cash", "title": "Ring of Fire"},
            {"artist": "Bad Bunny", "title": "Tití Me Preguntó"},
        ],
    },
    {
        "label": "sacred classique",
        "vibe_hint": "sacred classical choral requiem",
        "good": [
            {"artist": "Arvo Pärt",   "title": "Spiegel im Spiegel"},
            {"artist": "Hildegard von Bingen", "title": "O Virga ac Diadema"},
            {"artist": "Gabriel Fauré", "title": "Requiem"},
        ],
        "bad": [
            {"artist": "Post Malone", "title": "Circles"},
            {"artist": "Eminem",     "title": "Lose Yourself"},
            {"artist": "Lady Gaga",  "title": "Bad Romance"},
        ],
    },
]


def _load_config():
    path = os.path.join(ROOT, "config.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _banner(s):
    print("\n" + "=" * 72)
    print(s)
    print("=" * 72)


def main():
    config = _load_config()
    db = DB(os.path.join(ROOT, "music_agent.db"))
    sp = SpotifyClient(config)
    # will call attach_db via TasteDNA._load_cached
    taste = TasteDNA(sp, db)

    if not taste.profile:
        print("No taste snapshot cached — run the build first. Aborting.")
        return 1
    if not taste.genre_weights:
        print("NOTE: genre_weights is empty in snapshot. genre_affinity will "
              "return 0.5 neutral. Run TasteDNA.build() to regenerate with "
              "genre profile.")

    flow = FlowEngine()
    # PlaylistManager requires (spotify, db, flow)
    playlists = playlist_module.PlaylistManager(sp, db, flow)
    # minimal learner stub (not used by recommend_from_candidates)
    class _Learner:  # noqa: E306
        pass
    recommender = Recommender(sp, taste, flow, _Learner(), playlists)

    totals = {"verified": 0, "kept": 0, "genre_mismatch": 0, "bad_survived": 0,
              "good_dropped": 0}

    for case in TEST_CASES:
        _banner(f"VIBE: {case['label']}  (hint: {case['vibe_hint']})")
        candidates = case["good"] + case["bad"]
        res = recommender.recommend_from_candidates(
            candidates=candidates,
            count=6,
            playlist_name=None,
            existing_ids=None,
            flow_style="smooth",
            vibe_hint=case["vibe_hint"],
            library_fallback=False,  # isolate the gate
        )
        totals["verified"] += res.get("verified", 0)
        totals["kept"] += len(res.get("tracks", []))

        kept_keys = set()
        for t in res.get("tracks", []):
            kept_keys.add((t["artist"].lower(), t["name"].lower()))

        print(f"verified={res.get('verified')}  kept={len(res.get('tracks', []))}  "
              f"rejected={len(res.get('rejected', []))}")
        print("\n--- kept ---")
        for t in res.get("tracks", []):
            print(f"  [{t['score']:.2f}] g={t['genre_affinity']:.2f} "
                  f"era={t['era_fit']:.2f} pop={t['popularity_fit']:.2f} "
                  f"fam={t['familiarity']:.2f} — {t['artist']} — {t['name']} "
                  f"({t.get('year')}) genres={t.get('genres')[:3]}")

        print("\n--- rejected ---")
        for r in res.get("rejected", []):
            fit = r.get("fit")
            ga  = r.get("genre_affinity")
            extra = f" fit={fit:.2f} g={ga:.2f}" if fit is not None else ""
            print(f"  [{r['reason']}] {r['artist']} — {r['title']}{extra}")

        # tally expectations
        bad_titles = {(c["artist"].lower(), c["title"].lower()) for c in case["bad"]}
        good_titles = {(c["artist"].lower(), c["title"].lower()) for c in case["good"]}
        for r in res.get("rejected", []):
            if r.get("reason") == "genre_mismatch":
                totals["genre_mismatch"] += 1
        for t in res.get("tracks", []):
            # track's (artist, title) is the matched result, which may differ
            # in casing/punct — approximate by substring on the source-candidate
            # strings we submitted.
            hay = f"{t['artist'].lower()} {t['name'].lower()}"
            if any(c["artist"].lower() in hay and (
                    c["title"].lower().split()[0] in hay
                    or c["title"].lower() in hay) for c in case["bad"]):
                totals["bad_survived"] += 1

    _banner("TOTALS")
    for k, v in totals.items():
        print(f"  {k}: {v}")
    print("\nPASS criteria: genre_mismatch rejections > 0 AND bad_survived "
          "low relative to total bad candidates (15).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
