"""Flask server — routes the whole app."""
import json
import os
import sys
import threading
import webbrowser
from pathlib import Path

import traceback
from flask import Flask, request, jsonify, render_template

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import DB  # noqa: E402
from src.spotify import SpotifyClient  # noqa: E402
from src.taste import TasteDNA  # noqa: E402
from src.flow import FlowEngine  # noqa: E402
from src.recommend import Recommender  # noqa: E402
from src.learner import Learner  # noqa: E402
from src.brain import Brain  # noqa: E402
from src.playlist import PlaylistManager, DEFAULT_PROFILES  # noqa: E402
from src.taste_analyzer import (  # noqa: E402
    TasteAnalyzer, TasteAnalyzerError, PROFILE_JSON, PROFILE_MD, EXAMPLE_JSON,
)


CONFIG_PATH = ROOT / "config.json"
CONFIG_TEMPLATE = {
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "spotify_redirect_uri": "http://127.0.0.1:8888/callback",
    "anthropic_api_key": "",
    "openai_api_key": "",
}


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(CONFIG_TEMPLATE, indent=2))
    with CONFIG_PATH.open() as f:
        return json.load(f)


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ---------- bootstrap ----------
app = Flask(__name__, template_folder=str(ROOT / "templates"),
            static_folder=str(ROOT / "static"))

state = {"config": load_config(), "sp": None, "db": None, "taste": None,
         "flow": None, "recommender": None, "learner": None, "brain": None,
         "playlists": None, "last_recs": [], "initialized": False, "init_error": None}


def initialize():
    """Attempt to wire up all services. Returns (ok, error)."""
    try:
        cfg = state["config"]
        if not cfg.get("spotify_client_id") or not cfg.get("spotify_client_secret"):
            return False, "Missing Spotify credentials in config.json"
        db = DB(str(ROOT / "music_agent.db"))
        sp = SpotifyClient(cfg)
        flow = FlowEngine()
        playlists = PlaylistManager(sp, db, flow)
        taste = TasteDNA(sp, db)
        learner = Learner(db, taste)
        recommender = Recommender(sp, taste, flow, learner, playlists)
        brain = Brain(cfg, taste, learner, playlists)

        state.update({
            "sp": sp, "db": db, "flow": flow, "playlists": playlists,
            "taste": taste, "learner": learner, "recommender": recommender,
            "brain": brain, "initialized": True, "init_error": None,
        })
        return True, None
    except Exception as e:
        state["init_error"] = str(e)
        return False, str(e)


# ---------- health / settings ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    cfg = state["config"]
    return jsonify({
        "ready": state["initialized"],
        "has_spotify": bool(cfg.get("spotify_client_id") and cfg.get("spotify_client_secret")),
        "has_ai": bool(cfg.get("anthropic_api_key") or cfg.get("openai_api_key")),
        "init_error": state["init_error"],
        "taste_built": state["taste"].profile is not None if state["taste"] else False,
        "taste_summary": state["taste"].summary() if state["taste"] and state["taste"].profile else None,
        "feedback_stats": state["learner"].stats() if state["learner"] else None,
        "taste_profile_exists": PROFILE_MD.exists() and PROFILE_JSON.exists(),
    })


@app.route("/api/connect", methods=["POST"])
def api_connect():
    ok, err = initialize()
    return jsonify({"ok": ok, "error": err})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        data = request.get_json() or {}
        cfg = state["config"]
        for key in CONFIG_TEMPLATE:
            if key in data:
                cfg[key] = data[key]
        save_config(cfg)
        state["config"] = cfg
        return jsonify({"ok": True})
    # mask secrets on GET
    cfg = dict(state["config"])
    for k in ("spotify_client_secret", "anthropic_api_key", "openai_api_key"):
        v = cfg.get(k, "")
        cfg[k] = (v[:6] + "..." + v[-4:]) if len(v) > 12 else ""
    return jsonify(cfg)


# ---------- taste ----------
@app.route("/api/taste/build", methods=["POST"])
def api_taste_build():
    if not state["taste"]:
        return jsonify({"error": "not initialized"}), 400
    try:
        state["taste"].build()
        return jsonify({"ok": True, "summary": state["taste"].summary()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/taste")
def api_taste():
    if not state["taste"]:
        return jsonify({"error": "not initialized"}), 400
    if not state["taste"].profile:
        return jsonify({"built": False})
    return jsonify({"built": True, "summary": state["taste"].summary()})


@app.route("/api/taste/analyze", methods=["POST"])
def api_taste_analyze():
    """Run the LLM-powered taste analyzer.

    Pulls the user's full Spotify footprint, sends an aggregated stat block to
    the configured LLM, and writes both `taste_profile.json` and
    `TASTE_PROFILE.md` to the project root. This is distinct from
    /api/taste/build which is the lower-level statistical builder used for
    audio-feature scoring.
    """
    if not state["sp"]:
        return jsonify({"error": "not initialized — configure Spotify keys"}), 400
    cfg = state["config"]
    if not (cfg.get("anthropic_api_key") or cfg.get("openai_api_key")):
        return jsonify({"error": "no AI key configured — add Anthropic or OpenAI in Settings"}), 400
    try:
        analyzer = TasteAnalyzer(state["sp"], cfg)
        result = analyzer.analyze()
        return jsonify(result)
    except TasteAnalyzerError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/taste/profile")
def api_taste_profile():
    """Return the structured taste_profile.json (or the example if missing)."""
    path = PROFILE_JSON if PROFILE_JSON.exists() else EXAMPLE_JSON
    if not path.exists():
        return jsonify({"exists": False, "profile": None})
    try:
        return jsonify({
            "exists": PROFILE_JSON.exists(),
            "is_example": path == EXAMPLE_JSON,
            "profile": json.loads(path.read_text(encoding="utf-8")),
        })
    except Exception as e:
        return jsonify({"exists": False, "error": str(e)}), 500


# ---------- playlists ----------
@app.route("/api/playlists")
def api_playlists():
    if not state["playlists"]:
        return jsonify({"error": "not initialized"}), 400
    force = request.args.get("force") == "1"
    spotify_list = state["playlists"].list_from_spotify(force=force)
    # annotate with default managed profiles even if not yet on Spotify
    names_on_sp = {p["name"] for p in spotify_list}
    for name in DEFAULT_PROFILES:
        if name not in names_on_sp:
            spotify_list.append({
                "id": None, "name": name, "total": 0, "image": None,
                "is_managed": True, "not_yet_created": True,
            })
    return jsonify({"playlists": spotify_list})


@app.route("/api/playlist/<playlist_id>/tracks")
def api_playlist_tracks(playlist_id):
    if not state["sp"]:
        return jsonify({"error": "not initialized"}), 400
    try:
        tracks = state["sp"].get_playlist_tracks(playlist_id)
        return jsonify({"tracks": tracks})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlist/health", methods=["POST"])
def api_playlist_health():
    data = request.get_json() or {}
    name = data.get("playlist")
    if not name or not state["playlists"]:
        return jsonify({"error": "missing playlist or not initialized"}), 400
    return jsonify(state["playlists"].health(name))


@app.route("/api/playlist/clean", methods=["POST"])
def api_playlist_clean():
    data = request.get_json() or {}
    name = data.get("playlist")
    if not name or not state["playlists"]:
        return jsonify({"error": "missing playlist or not initialized"}), 400
    return jsonify(state["playlists"].clean(name))


@app.route("/api/playlist/reorder", methods=["POST"])
def api_playlist_reorder():
    data = request.get_json() or {}
    name = data.get("playlist")
    style = data.get("flow_style", "smooth")
    if not name or not state["playlists"]:
        return jsonify({"error": "missing playlist or not initialized"}), 400
    return jsonify(state["playlists"].reorder_for_flow(name, style=style))


@app.route("/api/playlist/add", methods=["POST"])
def api_playlist_add():
    data = request.get_json() or {}
    name = data.get("playlist")
    track_ids = data.get("track_ids") or []
    reorder = data.get("reorder", True)
    if not name or not state["playlists"]:
        return jsonify({"error": "missing playlist or not initialized"}), 400
    # record approvals for the tracks being added
    for tid in track_ids:
        match = next((r for r in state["last_recs"] if r["id"] == tid), None)
        if match and state["learner"]:
            state["learner"].record_feedback(match, "approved", playlist=name)
    result = state["playlists"].add_tracks(name, track_ids, reorder_all_for_flow=reorder)
    return jsonify(result)


@app.route("/api/playlist/remove", methods=["POST"])
def api_playlist_remove():
    data = request.get_json() or {}
    name = data.get("playlist")
    track_ids = data.get("track_ids") or []
    if not name or not state["playlists"]:
        return jsonify({"error": "missing playlist or not initialized"}), 400
    return jsonify(state["playlists"].remove_tracks(name, track_ids))


# ---------- recommendations ----------
@app.route("/api/recommend", methods=["POST"])
def api_recommend():
    data = request.get_json() or {}
    params = data.get("params") or {}
    playlist = data.get("playlist") or params.get("playlist")
    if playlist:
        params["playlist"] = playlist
    if not state["recommender"]:
        return jsonify({"error": "not initialized"}), 400

    existing_ids = []
    if playlist and state["playlists"]:
        p = state["playlists"].find_by_name(playlist)
        if p and p.get("id"):
            existing_ids = [t["id"] for t in state["sp"].get_playlist_tracks(p["id"])]

    recs = state["recommender"].recommend(params, existing_ids=existing_ids)
    state["last_recs"] = recs
    return jsonify({"tracks": recs, "params": params, "playlist": playlist})


@app.route("/api/recommend/feedback", methods=["POST"])
def api_recommend_feedback():
    data = request.get_json() or {}
    track_id = data.get("track_id")
    action = data.get("action", "rejected")
    reason = data.get("reason")
    playlist = data.get("playlist")
    if not track_id or not state["learner"]:
        return jsonify({"error": "missing track_id or not initialized"}), 400
    match = next((r for r in state["last_recs"] if r["id"] == track_id), None)
    track = match or {"id": track_id}
    state["learner"].record_feedback(track, action, reason=reason, playlist=playlist)
    return jsonify({"ok": True})


@app.route("/api/reference", methods=["POST"])
def api_reference():
    data = request.get_json() or {}
    url = data.get("url", "")
    if not url or not state["recommender"]:
        return jsonify({"error": "missing url or not initialized"}), 400
    params = state["recommender"].learn_from_reference(url)
    return jsonify({"params": params})


# ---------- chat ----------
def _resolve_playlist_name(requested):
    """Fuzzy-match a name against the user's Spotify playlists + default profiles.
    Falls back to matching against playlist DESCRIPTIONS so a vibe word like
    'trap' resolves to whichever playlist has 'trap' in its description."""
    if not requested or not state["playlists"]:
        return requested
    from src.playlist import PLAYLIST_DESCRIPTIONS
    req = str(requested).strip()
    req_low = req.lower()
    try:
        pls = state["playlists"].list_from_spotify()
    except Exception:
        pls = []
    names = [p["name"] for p in pls] + [n for n in DEFAULT_PROFILES if n not in {p["name"] for p in pls}]
    for n in names:
        if n == req:
            return n
    for n in names:
        if n.lower() == req_low:
            return n
    def strip_emoji(s):
        return "".join(c for c in s if c.isalnum() or c.isspace()).strip().lower()
    req_s = strip_emoji(req)
    if req_s:
        for n in names:
            ns = strip_emoji(n)
            if ns and (req_s in ns or ns in req_s):
                return n
        # description-based match: user says "trap" → whichever playlist has "trap" in its description.
        req_words = set(w for w in req_s.split() if len(w) >= 3)
        if req_words:
            for n in names:
                desc = (PLAYLIST_DESCRIPTIONS.get(n) or "").lower()
                if desc and any(w in desc for w in req_words):
                    return n
    return requested


def _dispatch_action(result):
    """Run the brain's action server-side. Returns a dict with the outcome,
    or None if nothing executable.
    """
    intent = result.get("intent") or "chat"
    if intent == "chat" or not result.get("auto_execute"):
        return None

    if intent in ("add_to_playlist", "suggest", "new_playlist", "refine"):
        return _dispatch_recommend(result)
    if intent == "clean":
        name = _resolve_playlist_name(result.get("target_playlist"))
        if not name:
            return {"ok": False, "error": "no playlist named"}
        r = state["playlists"].clean(name)
        return {"ok": True, "kind": "clean", "playlist": name, **r}
    if intent == "reorder":
        name = _resolve_playlist_name(result.get("target_playlist"))
        if not name:
            return {"ok": False, "error": "no playlist named"}
        r = state["playlists"].reorder_for_flow(name, style=result.get("flow_style", "smooth"))
        return {"ok": True, "kind": "reorder", "playlist": name, **r}
    if intent == "health":
        name = _resolve_playlist_name(result.get("target_playlist"))
        if not name:
            return {"ok": False, "error": "no playlist named"}
        r = state["playlists"].health(name)
        return {"ok": True, "kind": "health", "playlist": name, **r}
    if intent == "analyze_ref":
        url = result.get("ref_url")
        if not url:
            return {"ok": False, "error": "no reference url"}
        params = state["recommender"].learn_from_reference(url)
        return {"ok": True, "kind": "analyze_ref", "params": params}
    return None


def _dispatch_recommend(result):
    """Run the candidate-verification pipeline + optionally add to playlist."""
    intent = result.get("intent")
    candidates = result.get("candidate_tracks") or []
    audio_target = result.get("audio_target") or {}
    count = int(result.get("count") or 8)
    flow_style = result.get("flow_style", "smooth")
    search_queries = result.get("search_queries") or []

    # target playlist resolution (None for pure suggest / new_playlist without name)
    target = _resolve_playlist_name(result.get("target_playlist"))
    new_name = result.get("new_playlist_name")
    if intent == "new_playlist" and not target:
        target = new_name  # will be created on add
    existing_ids = []
    if target and state["playlists"]:
        p = state["playlists"].find_by_name(target)
        if p and p.get("id"):
            try:
                existing_ids = [t["id"] for t in state["sp"].get_playlist_tracks(p["id"])]
            except Exception:
                existing_ids = []

    pipeline = state["recommender"].recommend_from_candidates(
        candidates=candidates,
        audio_target=audio_target,
        count=count,
        playlist_name=target,
        existing_ids=existing_ids,
        flow_style=flow_style,
        search_queries=search_queries,
        library_fallback=True,
    )
    tracks = pipeline.get("tracks", [])
    state["last_recs"] = tracks

    out = {
        "ok": True,
        "kind": "recommend",
        "intent": intent,
        "playlist": target,
        "tracks": tracks,
        "verified": pipeline.get("verified", 0),
        "rejected": pipeline.get("rejected", []),
        "padded_from_library": pipeline.get("padded_from_library", 0),
        "candidate_count": pipeline.get("candidate_count", len(candidates)),
        "added": 0,
    }

    # auto-add for add_to_playlist / new_playlist / refine when a target is known
    if tracks and intent in ("add_to_playlist", "new_playlist", "refine") and target:
        ids = [t["id"] for t in tracks]
        try:
            add_res = state["playlists"].add_tracks(target, ids, reorder_all_for_flow=True)
            out["added"] = add_res.get("added", 0)
            out["playlist_id"] = add_res.get("playlist_id")
            # record approvals so learner evolves the profile
            if state["learner"]:
                for t in tracks:
                    try:
                        state["learner"].record_feedback(t, "approved", playlist=target)
                    except Exception:
                        pass
        except Exception as e:
            out["ok"] = False
            out["error"] = f"add failed: {type(e).__name__}: {e}"

    return out


@app.route("/api/chat", methods=["POST"])
def api_chat():
    try:
        data = request.get_json() or {}
        msg = (data.get("message") or "").strip()
        playlist_hint = data.get("playlist")
        if not msg:
            return jsonify({"error": "empty message"}), 400
        if not state["brain"]:
            return jsonify({"error": "not initialized — check Settings"}), 400

        history = state["db"].get_chat_history(limit=30) if state["db"] else []
        if state["db"]:
            state["db"].insert_chat("user", msg, context=playlist_hint)
        result = state["brain"].chat(msg, history=history, playlist_hint=playlist_hint)

        # canonicalize playlist name before dispatching
        if result.get("target_playlist"):
            result["target_playlist"] = _resolve_playlist_name(result["target_playlist"])

        # execute the action server-side if auto_execute
        try:
            action_result = _dispatch_action(result)
        except Exception as e:
            traceback.print_exc()
            action_result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if action_result is not None:
            result["action_result"] = action_result

        if state["db"]:
            state["db"].insert_chat("assistant", result.get("message", ""), context=playlist_hint)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "error": f"{type(e).__name__}: {e}",
            "message": f"Server error: {e}",
            "action": None,
            "questions": [],
        }), 500


@app.errorhandler(Exception)
def on_unhandled(e):
    traceback.print_exc()
    return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/chat/history")
def api_chat_history():
    if not state["db"]:
        return jsonify({"messages": []})
    return jsonify({"messages": state["db"].get_chat_history(limit=80)})


@app.route("/api/chat/clear", methods=["POST"])
def api_chat_clear():
    if state["db"]:
        state["db"].clear_chat()
    return jsonify({"ok": True})


# ---------- rules ----------
@app.route("/api/rules")
def api_rules():
    if not state["learner"]:
        return jsonify({"rules": []})
    return jsonify({"rules": state["learner"].get_active_rules(min_confidence=0.0)})


@app.route("/api/rules/<int:rule_id>", methods=["DELETE"])
def api_rule_delete(rule_id):
    if not state["db"]:
        return jsonify({"error": "not initialized"}), 400
    state["db"].delete_rule(rule_id)
    return jsonify({"ok": True})


# ---------- main ----------
def run():
    print("Music Agent v3")
    ok, err = initialize()
    if ok:
        print("  initialized")
    else:
        print(f"  init error: {err}")
        print("  configure Spotify keys at http://localhost:5000")

    url = "http://localhost:5000"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    run()
