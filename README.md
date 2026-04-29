# vibe-music-agent

Vibe to Spotify playlist, with zero hallucinated tracks.

A local Flask app that turns the way you talk about music ("late-night drive in the rain, melodic French rap, slow burners") into real Spotify playlists. The LLM proposes concrete tracks, the backend verifies every single one against the Spotify search API before anything is added, and a flow engine reorders the result for emotional shape instead of shuffle randomness. It also reads a personal taste profile built from your own library, so recommendations sound like *you* rather than the genre average.

What makes it different:

- **Zero-hallucination guarantee.** The LLM names tracks, the server re-searches each `(artist, title)` pair on Spotify and drops anything that does not token-match. No invented songs survive into your library. See `src/recommend.py`.
- **Taste-aware.** A statistical profile of your liked songs (top artists, genre weights, audio-feature distributions) is injected into every prompt, alongside a prose `TASTE_PROFILE.md` you can edit by hand. See `src/taste.py`.
- **Flow-aware ordering.** Tracks are sequenced by energy, tempo, key compatibility, valence and danceability, in five styles: `smooth`, `build`, `steady`, `journey`, `rollercoaster`. See `src/flow.py`.
- **Learns from feedback.** Every approval, rejection and correction goes into a local SQLite store; the learner extracts rules and evolves per-playlist profiles over time. See `src/learner.py`.

## Features

- Chat-driven playlist building, cleaning, and reordering
- Per-playlist "profiles" so vibes route to the right destination
- Reference-track URL ingestion ("more like this: <Spotify URL>")
- Duplicate detection and bulk cleanup
- Taste analyzer that turns your liked songs into an editable profile
- Anthropic Claude as the primary brain, OpenAI as a fallback
- Pure local app — no servers, no accounts beyond the ones you already have

## Architecture

The flow is one-way: a user's vibe enters as text, candidate tracks come back as JSON, and only verified Spotify track IDs ever reach the playlist API.

```
  User (browser)
       |
       v
  Web UI  (templates/index.html, static/app.js)
       |
       v
  Flask routes  (src/server.py)
       |
       v
  Brain  (src/brain.py)         <-- Anthropic Claude or OpenAI GPT
       |   proposes {artist,title} candidates
       v
  Recommender  (src/recommend.py)
       |   verifies each against Spotify search,
       |   token-matches, applies audio target,
       |   pads from library if needed
       v
  Flow Engine  (src/flow.py)    <-- orders for smooth/build/journey/...
       |
       v
  Playlist Manager  (src/playlist.py)
       |
       v
  Spotify API
```

The Learner (`src/learner.py`) sits alongside, recording every approval and rejection and feeding learned rules back into the Brain's system prompt on the next turn.

<!-- TASTE_ANALYZER_DOCS -->
## First-time setup

After installing dependencies, configuring keys, and authenticating with Spotify (see *Setup* below), the agent doesn't know who you are yet. Two artifacts make it personal:

- `taste_profile.json` — structured profile (cultural worlds, top artists per world, explicit ratio, popularity distribution, current direction, negative space, taste algorithm)
- `TASTE_PROFILE.md` — long-form prose portrait that mirrors the JSON in human language

Both are written to the project root by the **taste analyzer** the first time you run it. Both are gitignored.

To generate them, either:

- Click **Analyze my taste** on the banner that appears on first launch, or
- `POST /api/taste/analyze` (no body required), or
- Run from a Python shell:

  ```python
  from src.taste_analyzer import TasteAnalyzer
  from src.spotify import SpotifyClient
  import json
  cfg = json.load(open("config.json"))
  TasteAnalyzer(SpotifyClient(cfg), cfg).analyze()
  ```

The analyzer pulls your full liked-songs history, top artists across all three Spotify time ranges, top tracks across all three time ranges, and recently played. It aggregates that into a stat block (artist counts, genre histogram, language hints, explicit ratio, popularity buckets, monthly add histogram) and asks the configured LLM (Anthropic preferred, OpenAI fallback) to translate the numbers into a *cultural* portrait — worlds rather than genres, scenes rather than categories, with explicit attention to negative space (what you conspicuously *don't* listen to) and current direction.

Re-run the analyzer any time your library has drifted enough to warrant a fresh portrait. It overwrites both files atomically.

If you'd rather skip the LLM and hand-write your own profile, copy `TASTE_PROFILE.example.md` to `TASTE_PROFILE.md` and edit; the Brain reads whatever's there.
<!-- /TASTE_ANALYZER_DOCS -->

## Setup

1. Clone the repo.

   ```
   git clone https://github.com/crownkrebs/vibe-music-agent.git
   cd vibe-music-agent
   ```

2. Install dependencies.

   ```
   pip install -r requirements.txt
   ```

3. Get Spotify Developer credentials at https://developer.spotify.com/dashboard. Create an app and set the redirect URI to exactly `http://127.0.0.1:8888/callback`. Copy the Client ID and Client Secret.

4. Get an Anthropic API key at https://console.anthropic.com/ **or** an OpenAI key at https://platform.openai.com/api-keys. At least one is required. Anthropic is preferred — Claude produces tighter, more musically-grounded JSON in practice.

5. Copy `config.example.json` to `config.json` and fill in the keys you have. Leave the others blank.

6. Run the server:

   ```
   python src/server.py
   ```

   On Windows you can also double-click `run.bat`.

7. Open http://localhost:5000. The first time you do anything that hits the Spotify API, you will be sent through the Spotify OAuth consent screen. Approve it; the token is cached locally in `.spotify_cache`.

8. **Personalize.** Click *Analyze My Taste* in the UI. This reads your Spotify library and writes two files to the project root:

   - `taste_profile.json` — structured profile (top artists, genre weights, audio-feature distributions)
   - `TASTE_PROFILE.md` — a prose summary of your cultural-musical worlds

   Both files are gitignored. They are what makes recommendations feel like *you* instead of the genre average — without them, the agent falls back to generic taste. Reference shapes are checked into the repo as `taste_profile.example.json` and `TASTE_PROFILE.example.md` so you can see the schema. Edit either file freely; the LLM reads them as context on every turn.

9. **(Optional) Define your playlist categories.** Copy `playlists.example.json` to `playlists.json` and edit. Each entry is a name (with optional emoji) plus a one-line description like `"High-BPM, aggressive, gym energy"`. The LLM uses these descriptions to route a vibe to the right playlist — saying "I need something for the gym" can resolve to "🏃 Workout" because the description mentions gym energy. `playlists.json` is gitignored.

## Personalization

Two files turn this from a generic recommender into a personal one. Both live at the project root, both are gitignored.

**Taste profile** — `TASTE_PROFILE.md` and `taste_profile.json`.

Generated by the analyzer (`src/taste_analyzer.py`) the first time you click *Analyze My Taste*. The Markdown file describes your cultural-musical worlds in prose (e.g. "Late-night electronic: ambient + IDM, headphone listens, no vocals"), the JSON file holds the numeric profile (top artists, genre weights, audio-feature percentiles). Edit the Markdown freely — add a section about a niche scene you care about, delete things that misread you. The Brain reads both as context on every turn.

**Playlist profiles** — `playlists.json`.

Your named playlist categories. Each one is `{ name, description, defaults }` where `defaults` is an audio-feature target (energy, valence, tempo, danceability, acousticness) used as the starting point before learning kicks in. The descriptions matter: when you say "drop something dark in there", the LLM matches that vibe against the descriptions to pick a playlist. See `src/playlist.py:DEFAULT_PROFILES` for the schema.

## Usage examples

A few things you can type in the chat:

- `make me a 30-track playlist for a late-night drive in the rain`
  Builds candidates, verifies them on Spotify, orders for smooth flow, asks for confirmation, then adds to a Late Night playlist.

- `clean my Workout playlist of duplicates`
  Runs the dedupe pass on that playlist (different Spotify IDs, same canonical artist + title).

- `reorder my Late Night playlist for smoother flow`
  Re-sequences with the `smooth` flow strategy without changing membership.

- `add 10 more like this to Discovery: https://open.spotify.com/track/...`
  Treats the URL as a reference, extracts a vibe target, finds neighbors, verifies, adds.

- `what's in my library that sounds like Frank Ocean's Blonde?`
  Pure suggest mode — proposes from your library and similar artists, no playlist write.

- `make a new playlist called Sunday Coffee, jazzy and warm, 25 tracks`
  Creates the playlist, builds it, adds.

## Tech stack

- **Flask** — local HTTP server and routing
- **Spotipy** — Spotify Web API client
- **Anthropic Claude** (Opus / Sonnet) — primary brain
- **OpenAI GPT** — fallback brain
- **SQLite** — local store for chat history, feedback, learned rules, and per-playlist evolved profiles

## Privacy

Everything runs on your machine. Your Spotify tokens, taste profile, feedback history, chat history, and playlist categories all live inside the project directory. No telemetry. The only outbound network calls are to:

- `api.spotify.com` (search, library reads, playlist writes)
- `api.anthropic.com` (if you use Claude)
- `api.openai.com` (if you use GPT)

If you want to wipe state, see `CONFIG.md`.

## Contributing

PRs welcome. See `CONTRIBUTING.md` for layout, conventions, and how to add new LLM providers, flow styles, or intents.

## License

MIT — see `LICENSE`.
