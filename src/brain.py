"""AI curator — parses the user's free-form request into a verifiable action.
The LLM proposes concrete tracks (artist+title); the server verifies every one
against Spotify before anything is added. Zero hallucinated tracks survive.
"""
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TASTE_PROFILE_MD = ROOT / "TASTE_PROFILE.md"


SYSTEM_PROMPT = """You are the user's personal music curator. The user types to you in natural language; you ALWAYS reply in English, unless the user has set a different preferred language in their taste profile.

WHO THE USER IS (their cultural taste portrait — read this first)
{taste_profile}

USER'S LIBRARY RIGHT NOW
{taste_summary}

USER'S ACTIVE PLAYLISTS
{playlist_catalog}

{playlist_hint}

LEARNED PATTERNS & RECENT FEEDBACK
{learned_context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CORE CONTRACT

Return ONE valid JSON object, nothing else. No prose, no markdown fences. Every key below must be present — if unknown, use the default shown.

{
  "message": "",                      // short, warm, terse, ALWAYS ENGLISH
  "intent": "chat",                   // one of the 9 intents below
  "target_playlist": "",              // exact name from catalog, or ""
  "new_playlist_name": "",            // only set when creating
  "vibe": "",                         // internal feel description
  "candidate_tracks": [],             // [{artist, title}, ...]
  "audio_target": {},                 // cosmetic — leave {} (audio features deprecated)
  "count": 8,
  "auto_execute": false,
  "search_queries": [],
  "flow_style": "smooth",
  "ref_url": "",
  "questions": []
}

Intents: add_to_playlist | suggest | new_playlist | refine | chat | clean | reorder | health | analyze_ref.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LANGUAGE RULE (non-negotiable)

The "message" field is in ENGLISH at all times, regardless of what language the user wrote in. Short, warm, low ego. No corporate "I'd be happy to" tone either. Think: a friend with taste, in English.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CANDIDATE QUALITY (how you earn trust)

1. Every (artist, title) pair must be a REAL, KNOWN track. The server re-searches each one against Spotify and drops mismatches silently. Do not invent titles. If you aren't sure a song exists, don't name it — prefer a deep cut from a well-known artist over a best-guess from an obscure one.

2. OVER-PROPOSE. Because the verification gate drops some, propose 1.5× the requested count. count=8 → 12 candidates. count=10 → 15. count=12 → 18. Cap at 20.

3. ARTIST-LEVEL FIT beats random indie. Use the top-artists and recent-likes injected above as your ONLY taste signal — they are the truth. If the user likes Tame Impala, neighbors are Pond, MGMT, Unknown Mortal Orchestra, Mac DeMarco. Anchor 2-3 tracks on the named/implied artist, then 6-8 on close neighbors, 2-3 deeper cuts.

4. When the user references a scene with no clean Spotify tag (obscure regional or vintage music, niche subgenres), still fill candidate_tracks with real pairs you're confident about; put any extra hints in search_queries.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROTECTED PLAYLISTS

Some playlists in the catalog above may be marked EXCLUSIVE in their description (e.g. an artist-only vault, or a sacred archive of vintage vocalists). For those:
- only add tracks that fit the explicit constraint
- if the user asks you to add an artist that obviously belongs to a protected playlist into a different playlist, ASK FIRST instead of acting:
  - intent = "chat"
  - message = polite English clarifying question, e.g. "That artist usually lives in your Classics playlist — did you mean there instead?"
  - candidate_tracks = []
  - auto_execute = false
  - questions = [{"text": "Did you mean the Classics playlist?", "options": ["Yes, send it there", "No, skip it"]}]
- do NOT silently slip a clearly-protected artist into the wrong playlist.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEGRADE RULES

A) Refine with no prior state. If the user's message reads like "refine" ("more energy", "too chill", "variety", "same but darker") BUT the chat history contains no prior assistant turn with candidate_tracks, you CANNOT refine a ghost. Degrade to:
   - intent = "suggest"
   - candidate_tracks = []
   - auto_execute = false
   - questions = one short clarifying question ("What should I start from — a playlist, an artist, or a mood?")

B) Vague anchor. If the ask is extremely vague ("gimme some songs", "surprise me", "something good", "music"), do NOT guess. Set:
   - intent = "chat"
   - candidate_tracks = []
   - auto_execute = false
   - questions = ONE short question with 2-4 tight options (mood? playlist? era? single vibe word?)
   Max one question. Don't interrogate.

C) add_to_playlist with no candidates. If you set intent="add_to_playlist" but can't produce real candidates, DEMOTE to intent="chat" + one question. A silent no-op is worse than asking.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
auto_execute SEMANTICS

Set auto_execute=true ONLY when the user gave clear consent to act — "add them", "yes do it", "send it to X", "build the playlist", "go".

Set auto_execute=false for suggest-mode previews, clarifying questions, refine-previews, and any chat turn. When in doubt, false.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTENT GUIDE (one example each)

add_to_playlist — the user names both a scene AND a target playlist. target_playlist set, candidate_tracks filled (1.5× count), auto_execute=true if they consented.
  Example: "add some hard trap to my workout playlist" →
  {"message":"Locked. 12 hard trap cuts incoming for 🏃 Workout.","intent":"add_to_playlist","target_playlist":"🏃 Workout","candidate_tracks":[{"artist":"Travis Scott","title":"FE!N"}, ... 11 more ...],"count":8,"auto_execute":true, ...}

suggest — "recommend something", "what you got". candidate_tracks filled, target_playlist="", auto_execute=false (preview).
  Example: "recommend some late-night rap" → intent="suggest", 12 candidates, auto_execute=false.

new_playlist — "make me a playlist for X". new_playlist_name set (short, no emoji unless the user uses one), candidate_tracks filled, auto_execute=true on clear consent.

refine — "more energy", "darker", "variety". Requires a prior assistant turn with candidates in history. target_playlist carried from last turn, candidate_tracks optionally new, auto_execute=false by default.

chat — pure conversation, clarifying question, or refusal. candidate_tracks=[], auto_execute=false.

clean / reorder / health — structural ops on target_playlist. No candidates needed.

analyze_ref — the user pasted a Spotify/YouTube URL to learn from. ref_url set, candidate_tracks=[].

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PLAYLIST MATCHING

Match the catalog generously. Use the description text in USER'S ACTIVE PLAYLISTS above to map vibe words to the right playlist (e.g. "gym"/"workout"/"hard" → the workout-style playlist, "cooking"/"dinner" → the warm/jazzy one, "late"/"moody" → the night/atmospheric one). If two playlists are plausible, pick the closer one by vibe and move on — don't ask unless it's truly a coin-flip.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FINAL CHECKLIST (before you emit)

- Every schema key present? (even empty strings / arrays / {})
- message in ENGLISH?
- candidate_tracks count ≈ 1.5× count when action intent?
- No clearly-protected/exclusive-playlist artists landing in the wrong playlist?
- auto_execute=true only on clear consent?
- If intent=add_to_playlist, candidate_tracks non-empty (or demoted to chat)?
- If intent=refine, a prior assistant candidate turn exists (or degraded to suggest+question)?
"""


class Brain:
    def __init__(self, config, taste, learner, playlist_manager):
        self.config = config
        self.taste = taste
        self.learner = learner
        self.playlists = playlist_manager
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

    # ---------- prompt construction ----------
    def _build_system_prompt(self, playlist_hint=None):
        try:
            summary = self._taste_summary_text()
        except Exception as e:
            summary = f"(taste summary unavailable: {e})"
        try:
            catalog = self._playlist_catalog_text()
        except Exception as e:
            catalog = f"(playlist catalog unavailable: {e})"
        try:
            learned = self.learner.get_taste_context(max_rules=6, max_recent=6)
        except Exception as e:
            learned = f"(feedback unavailable: {e})"
        pl_hint = ""
        if playlist_hint:
            try:
                profile = self.playlists.get_profile(playlist_hint)
                if profile:
                    pl_hint = (f"CURRENT CONTEXT: the user has the \"{playlist_hint}\" playlist open right now. "
                               f"Target profile: {json.dumps(profile, ensure_ascii=False)}. "
                               f"Default target_playlist to this name unless they name another.")
            except Exception:
                pass
        try:
            taste_profile = self._taste_profile_md()
        except Exception:
            taste_profile = "(no detailed taste profile yet — run /api/taste/analyze)"
        return (SYSTEM_PROMPT
                .replace("{taste_profile}", str(taste_profile))
                .replace("{taste_summary}", str(summary))
                .replace("{playlist_catalog}", str(catalog))
                .replace("{playlist_hint}", str(pl_hint))
                .replace("{learned_context}", str(learned) or "No feedback yet."))

    def _taste_profile_md(self, max_chars=8000):
        """Inject the long-form TASTE_PROFILE.md if the user has run the
        analyzer. Truncated to keep the system prompt bounded."""
        if not TASTE_PROFILE_MD.exists():
            return "(no detailed taste profile yet — run the taste analyzer to generate one)"
        try:
            text = TASTE_PROFILE_MD.read_text(encoding="utf-8")
        except Exception as e:
            return f"(taste profile unreadable: {e})"
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[...profile truncated for prompt budget...]"
        return text

    def _taste_summary_text(self):
        s = self.taste.summary()
        if not s.get("total"):
            return "Profile not yet built — taste DNA unknown."
        c = s.get("center", {})
        trend = s.get("recent_direction", {})
        top = s.get("top_artists", [])[:30]
        top_fmt = ", ".join(f"{n}({k})" for n, k in top)
        recent_artists = trend.get("recent_top_artists", [])[:15]
        recent_fmt = ", ".join(f"{n}({k})" for n, k in recent_artists) if recent_artists else "—"
        return (
            f"{s['total']} liked songs analyzed.\n"
            f"Center: energy {c.get('energy', 0):.2f}, valence {c.get('valence', 0):.2f}, "
            f"dance {c.get('danceability', 0):.2f}, tempo {c.get('tempo', 0):.0f} BPM, "
            f"acoustic {c.get('acousticness', 0):.2f}.\n"
            f"Recent drift — energy {trend.get('energy_shift', 0):+.2f}, "
            f"valence {trend.get('valence_shift', 0):+.2f}, "
            f"tempo {trend.get('tempo_shift', 0):+.0f} BPM.\n"
            f"Top artists (all-time, with like-count): {top_fmt}.\n"
            f"Recently liked artists: {recent_fmt}."
        )

    def _playlist_catalog_text(self):
        try:
            from src.playlist import DEFAULT_PROFILES, PLAYLIST_DESCRIPTIONS
        except Exception:
            DEFAULT_PROFILES, PLAYLIST_DESCRIPTIONS = {}, {}
        lines = []
        for name, prof in DEFAULT_PROFILES.items():
            desc = PLAYLIST_DESCRIPTIONS.get(name, "")
            lines.append(
                f"  \"{name}\" — {desc}. "
                f"energy {prof.get('energy', 0):.2f}, valence {prof.get('valence', 0):.2f}, "
                f"tempo {prof.get('tempo', 0):.0f}."
            )
        return "\n".join(lines) if lines else "(no managed playlists configured)"

    # ---------- main ----------
    def chat(self, user_message, history=None, playlist_hint=None):
        """Returns parsed dict with the new schema."""
        system = self._build_system_prompt(playlist_hint=playlist_hint)
        history = history or []

        errors = []
        raw = None
        if self.anthropic:
            raw, err = self._call_anthropic(system, history, user_message)
            if err:
                errors.append(f"Anthropic: {err}")
        if raw is None and self.openai:
            raw, err = self._call_openai(system, history, user_message)
            if err:
                errors.append(f"OpenAI: {err}")
        if raw is None:
            detail = "; ".join(errors) if errors else "no AI keys configured"
            return self._empty(f"AI offline — {detail}")
        return self._parse(raw, playlist_hint=playlist_hint)

    def _normalize_history(self, history):
        msgs = []
        for h in history[-20:]:
            role = "user" if h.get("role") == "user" else "assistant"
            content = (h.get("content") or "").strip()
            if not content:
                continue
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n" + content
            else:
                msgs.append({"role": role, "content": content})
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    def _call_anthropic(self, system, history, user_message):
        msgs = self._normalize_history(history)
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] += "\n" + user_message
        else:
            msgs.append({"role": "user", "content": user_message})
        last_err = None
        for model in (
            "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
            "claude-opus-4-5", "claude-sonnet-4-5", "claude-3-5-sonnet-latest",
            "claude-3-5-haiku-latest",
        ):
            try:
                resp = self.anthropic.messages.create(
                    model=model,
                    max_tokens=1800,
                    system=system,
                    messages=msgs,
                )
                return resp.content[0].text, None
            except Exception as e:
                last_err = f"{model}: {type(e).__name__}: {str(e)[:180]}"
                continue
        return None, last_err

    def _call_openai(self, system, history, user_message):
        try:
            msgs = [{"role": "system", "content": system}]
            for h in history[-20:]:
                msgs.append({"role": h.get("role", "user"),
                             "content": h.get("content") or ""})
            msgs.append({"role": "user", "content": user_message})
            resp = self.openai.chat.completions.create(
                model="gpt-4o",
                max_tokens=1800,
                messages=msgs,
                response_format={"type": "json_object"},
            )
            return resp.choices[0].message.content, None
        except Exception as e:
            return None, f"{type(e).__name__}: {str(e)[:180]}"

    # ---------- parsing ----------
    _VALID_INTENTS = {
        "add_to_playlist", "suggest", "new_playlist", "refine",
        "chat", "clean", "reorder", "health", "analyze_ref",
    }

    def _parse(self, raw, playlist_hint=None):
        if not raw:
            return self._empty("AI returned nothing.")
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        m = re.search(r"\{[\s\S]*\}", text)
        data = None
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
        if not isinstance(data, dict):
            # degrade gracefully — treat as plain chat
            return self._empty(text[:400])

        message = str(data.get("message") or "").strip()
        intent = str(data.get("intent") or "chat").strip()
        if intent not in self._VALID_INTENTS:
            intent = "chat"
        target_playlist = data.get("target_playlist") or playlist_hint or None
        new_playlist_name = data.get("new_playlist_name") or None
        vibe = str(data.get("vibe") or "").strip()

        candidates = []
        for c in data.get("candidate_tracks") or []:
            if not isinstance(c, dict):
                continue
            artist = str(c.get("artist") or "").strip()
            title = str(c.get("title") or "").strip()
            if artist and title:
                candidates.append({"artist": artist, "title": title})

        audio_target = self._sanitize_audio_target(data.get("audio_target") or {})
        count = int(data.get("count") or 8)
        count = max(1, min(50, count))

        auto_execute = bool(data.get("auto_execute", True))
        # Safety: never auto-execute on intents without required fields
        if intent in ("add_to_playlist", "clean", "reorder", "health") and not target_playlist:
            auto_execute = False
        if intent == "analyze_ref" and not data.get("ref_url"):
            auto_execute = False
        if intent == "new_playlist" and not new_playlist_name:
            # allow — dispatcher will generate a default name from vibe
            pass
        if intent in ("add_to_playlist", "suggest", "new_playlist") and not candidates:
            auto_execute = False
            if not (data.get("questions") or []):
                intent = "chat"
                if not message:
                    message = ("Not enough to go on yet — what style, mood, "
                               "or artists are you in the mood for?")

        questions = []
        for q in data.get("questions") or []:
            if isinstance(q, dict) and q.get("text"):
                questions.append({
                    "text": str(q["text"]),
                    "options": [str(o) for o in (q.get("options") or []) if o][:6],
                })

        return {
            "message": message,
            "intent": intent,
            "target_playlist": target_playlist,
            "new_playlist_name": new_playlist_name,
            "vibe": vibe,
            "candidate_tracks": candidates,
            "audio_target": audio_target,
            "count": count,
            "auto_execute": auto_execute,
            "search_queries": [str(s) for s in (data.get("search_queries") or []) if s][:10],
            "flow_style": str(data.get("flow_style") or "smooth"),
            "ref_url": data.get("ref_url") or None,
            "questions": questions,
            # legacy: keep for backward compat with older frontend paths
            "action": self._legacy_action(intent, target_playlist, new_playlist_name,
                                          data, audio_target, count),
        }

    def _sanitize_audio_target(self, raw):
        out = {}
        for key, default_hi in (("energy", 1.0), ("valence", 1.0), ("danceability", 1.0),
                                ("acousticness", 1.0), ("instrumentalness", 1.0),
                                ("speechiness", 1.0)):
            v = raw.get(key)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                try:
                    lo, hi = float(v[0]), float(v[1])
                    lo = max(0.0, min(default_hi, lo))
                    hi = max(0.0, min(default_hi, hi))
                    if lo > hi:
                        lo, hi = hi, lo
                    out[key] = [round(lo, 3), round(hi, 3)]
                except Exception:
                    pass
        tempo = raw.get("tempo")
        if isinstance(tempo, (list, tuple)) and len(tempo) == 2:
            try:
                lo, hi = float(tempo[0]), float(tempo[1])
                lo = max(40.0, min(220.0, lo))
                hi = max(40.0, min(220.0, hi))
                if lo > hi:
                    lo, hi = hi, lo
                out["tempo"] = [round(lo, 1), round(hi, 1)]
            except Exception:
                pass
        return out

    def _legacy_action(self, intent, target_playlist, new_playlist_name, data, audio_target, count):
        """Minimal legacy action dict so pre-existing frontend paths still behave."""
        if intent in ("add_to_playlist", "suggest", "new_playlist", "refine"):
            return {
                "type": "recommend",
                "playlist": target_playlist or new_playlist_name,
                "params": {
                    **audio_target,
                    "count": count,
                    "flow_style": data.get("flow_style", "smooth"),
                    "search_queries": data.get("search_queries") or [],
                },
            }
        if intent == "clean":
            return {"type": "clean", "playlist": target_playlist}
        if intent == "reorder":
            return {"type": "reorder", "playlist": target_playlist,
                    "flow_style": data.get("flow_style", "smooth")}
        if intent == "health":
            return {"type": "health", "playlist": target_playlist}
        if intent == "analyze_ref":
            return {"type": "learn_reference", "url": data.get("ref_url")}
        return None

    def _empty(self, msg):
        return {
            "message": msg or "",
            "intent": "chat",
            "target_playlist": None,
            "new_playlist_name": None,
            "vibe": "",
            "candidate_tracks": [],
            "audio_target": {},
            "count": 8,
            "auto_execute": False,
            "search_queries": [],
            "flow_style": "smooth",
            "ref_url": None,
            "questions": [],
            "action": None,
        }
