# Contributing

Thanks for looking. This is a small, opinionated codebase — please keep changes focused and stay in line with the existing style.

## Running locally for development

Same as the README setup:

1. `git clone https://github.com/crownkrebs/vibe-music-agent.git && cd vibe-music-agent`
2. `pip install -r requirements.txt`
3. Create a Spotify app at https://developer.spotify.com/dashboard, redirect URI `http://127.0.0.1:8888/callback`.
4. Get an Anthropic key (https://console.anthropic.com/) or OpenAI key (https://platform.openai.com/api-keys).
5. Copy `config.example.json` to `config.json` and fill in the keys.
6. `python src/server.py` (or `run.bat` on Windows).
7. Open http://localhost:5000, complete OAuth, click *Analyze My Taste* to generate `taste_profile.json` + `TASTE_PROFILE.md`.
8. Optionally copy `playlists.example.json` to `playlists.json` and edit your categories.

For dev work it helps to run with a dedicated test Spotify account so you do not splatter experimental playlists across your main library.

## Project layout

```
src/
  server.py          Flask app, all HTTP routes, action dispatch
  brain.py           LLM call layer (Anthropic + OpenAI fallback) and system prompt
  recommend.py       Spotify-side verification + library-fallback pipeline
  taste.py           Builds the statistical taste profile from liked songs
  taste_analyzer.py  Generates TASTE_PROFILE.md + taste_profile.json
  flow.py            Track-order optimizer (smooth, build, steady, journey, rollercoaster)
  learner.py         Feedback recording, rule extraction, profile evolution
  playlist.py        Playlist CRUD, profiles, dedupe, reorder
  spotify.py         Spotipy wrapper with token cache + retry logic
  vibe.py            Vibe param extraction, audio-feature math
  db.py              SQLite schema and access
templates/index.html The single-page web UI
static/app.js        Frontend chat + playlist UI
static/style.css     Styles
tests/               pytest suite
```

## How to add a new LLM provider

The brain abstracts providers behind two small init methods. To add a new one, follow the existing template in `src/brain.py`:

- Look at `src/brain.py:_init_anthropic` (line 147) and `src/brain.py:_init_openai` (line 157). Each returns a client or `None` if its key is missing.
- Add `_init_yourprovider` that does the same, reading from `config.get("yourprovider_api_key")`.
- Wire it into `Brain.__init__` next to the existing two clients.
- In the call path that picks a provider, fall through to your client when the prior ones are unavailable. Match the existing JSON-only contract — your provider must return a single JSON object matching the schema in `SYSTEM_PROMPT`. If your model needs a different prompt phrasing, do that work in a per-provider builder rather than forking the whole prompt.
- Add the new key to `CONFIG_TEMPLATE` in `src/server.py` so it round-trips through the settings UI.

## How to add a new flow style

`src/flow.py` defines the `FlowEngine`. To add a style:

- Append your style name to `FlowEngine.STYLES`.
- Add a private method `_yourstyle(self, tracks)` that takes a list of track dicts (each with `energy`, `valence`, `danceability`, `tempo`, optionally `key`/`mode`) and returns an ordered list. Use `self.transition_score(a, b)` if you want the existing key/tempo/energy cost function.
- Wire it into the `method = {...}` dispatch in `FlowEngine.order`.
- Surface it in the UI: the dropdown in `static/app.js` lists style names, and the chat path passes `flow_style` through `src/server.py:_dispatch_action`.

## How to add a new playlist action / intent

The brain's intents are listed in `src/brain.py` (search the system prompt for "Intents:"). To add one:

- Add the new intent name to the system prompt's intent enum, and document when it should fire.
- Handle it in `src/server.py:_dispatch_action` (line 317). Keep the return shape consistent (`{ok, kind, ...}`) so the frontend can render it generically.
- If it needs a new HTTP route, add one alongside the others in `src/server.py`.
- Update the frontend in `static/app.js` if the action's result needs custom rendering. Otherwise the default chat-bubble render will pick it up.

## Tests

```
pytest tests/
```

Add tests next to the area you change. The verification logic in `src/recommend.py` is the most important to keep covered — `tests/test_match.py` is the existing reference.

## Code style

- Stay in line with the existing code. Small functions, lowercase docstrings, no class hierarchies for their own sake.
- No big formatter sweeps in PRs. If you want to introduce `ruff` / `black`, open a discussion first and do it as its own PR.
- Type hints are nice but not required. Match the surrounding file.
- Keep imports stdlib-first, third-party-second, local-third.

## Commit messages

Short, imperative. Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`) is encouraged but not required. Examples:

- `feat: add 'rollercoaster' flow style`
- `fix: token-match drops featured-artist suffixes`
- `docs: clarify redirect URI in setup`

## PR checklist

Before opening a PR:

- [ ] `pytest tests/` passes
- [ ] No credentials committed (`config.json`, API keys, OAuth tokens)
- [ ] No personal taste data committed (`TASTE_PROFILE.md`, `taste_profile.json`)
- [ ] No personal playlist categories committed (`playlists.json`)
- [ ] No local databases committed (`*.db`)
- [ ] No log files committed (`*.log`)
- [ ] Touched docs (`README.md`, `CONFIG.md`) if behavior or config changed

## Code of conduct

Be kind. No harassment, no slurs, no bad-faith arguments. Focus on the music and the code. If a conversation goes sideways, take a walk. Maintainers may remove comments or contributors that violate this.
