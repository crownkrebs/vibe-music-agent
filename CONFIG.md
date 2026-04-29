# Configuration reference

This is the deeper config doc. Read the README first for the happy-path setup; come here when something is off, when you want to know exactly what each file does, or when you want to reset state.

## `config.json`

The only file you have to edit by hand. Created automatically with empty values on first run if missing — the template lives in `src/server.py:CONFIG_TEMPLATE`.

| Key | Required | Description |
|---|---|---|
| `spotify_client_id` | yes | Client ID from your Spotify Developer Dashboard app. Get it at https://developer.spotify.com/dashboard. |
| `spotify_client_secret` | yes | Client Secret from the same Spotify app. Treat it as a password. |
| `spotify_redirect_uri` | yes | Must match the redirect URI registered in your Spotify app exactly, character for character. Default: `http://127.0.0.1:8888/callback`. If you change one side, change both. |
| `anthropic_api_key` | one of the two | Anthropic API key from https://console.anthropic.com/. **Preferred** — Claude (Opus/Sonnet) is more reliable at the structured-JSON output the brain expects, and produces better musical reasoning in practice. |
| `openai_api_key` | one of the two | OpenAI API key from https://platform.openai.com/api-keys. Used as a fallback when `anthropic_api_key` is missing or fails. Works, but expect more verification rejections from the verifier (more hallucinated track titles). |

You only need one of `anthropic_api_key` / `openai_api_key`. If both are present, Anthropic wins. If both are missing the server boots but the chat endpoint will refuse with `not initialized — check Settings`.

`config.json` is **gitignored**. Never commit it. The settings UI exposes a masked GET so you can confirm something is set without leaking the value.

## Files at the project root

All of these live next to `config.json`. None are checked into git. All are created on demand.

| File | Created by | Purpose |
|---|---|---|
| `config.json` | first server boot, or copying `config.example.json` | Secrets and the Spotify redirect URI. See above. |
| `taste_profile.json` | clicking *Analyze My Taste* in the UI (writes from `src/taste_analyzer.py`) | Structured taste profile: top artists, genre weights, audio-feature percentiles, era distribution. The LLM reads a serialized form on every turn. Schema mirrored in `taste_profile.example.json`. |
| `TASTE_PROFILE.md` | same analyzer run | Prose version of the same profile — your cultural-musical worlds in plain English. Edit freely; the LLM reads it as context. Mirrored in `TASTE_PROFILE.example.md`. |
| `playlists.json` | you, by copying `playlists.example.json` | Your named playlist categories. Each entry has a name (emoji optional), a one-line description (used by the LLM to route vibes), and a default audio-feature target. The defaults evolve over time as the learner records approvals. |
| `music_agent.db` | first server boot | Local SQLite database. Holds chat history, feedback rows (approve/reject/skip), learned rules, evolved per-playlist profiles, and an artist-genre cache. |
| `.spotify_cache` / `.cache` | first Spotify OAuth | Spotify access + refresh tokens, written by Spotipy. Either filename can appear depending on the Spotipy version; both are gitignored. |

## Resetting state

All resets are "delete the file and let it regenerate" — there is no destructive admin command.

- **Wipe chat + feedback history (keep auth and taste).** Delete `music_agent.db`. Next request rebuilds the schema with empty tables. You will lose: chat history, all feedback rows, learned rules, evolved playlist profiles. The default profiles in `src/playlist.py:DEFAULT_PROFILES` will apply again.

- **Re-authenticate Spotify.** Delete `.spotify_cache` (and `.cache` if present). The next Spotify-touching request will trigger the OAuth consent screen again. Useful if you switched Spotify accounts or revoked the app's access.

- **Regenerate taste profile.** Delete `taste_profile.json` and `TASTE_PROFILE.md`. Click *Analyze My Taste* again. Useful if your library has shifted significantly and the old profile is no longer representative — or if you hand-edited the Markdown into a corner and want to start over.

- **Reset playlist categories.** Delete `playlists.json`. Without it the app falls back to the defaults in `src/playlist.py:DEFAULT_PROFILES` (which include the original author's categories — copy `playlists.example.json` and edit instead if you want your own).

- **Full nuke.** Delete `config.json`, `music_agent.db`, `.spotify_cache`, `.cache`, `taste_profile.json`, `TASTE_PROFILE.md`, `playlists.json`. You are now back to a fresh clone, minus the source files. Walk through the README setup again.

## Notes

- The Spotify redirect URI port (`8888`) is unrelated to the app's own port (`5000`). Both are loopback. If you change the redirect URI port, change it in both your Spotify Developer app and `config.json`.
- The OAuth scopes the app requests are defined in `src/spotify.py`. They include playlist read/write and library read. If you add a feature that needs a new scope, delete `.spotify_cache` after the change so the next OAuth flow re-prompts with the wider scope.
- If you want to use a non-default config path (e.g. for multiple profiles), the path is hardcoded to `config.json` at the project root in `src/server.py:CONFIG_PATH`. Patch it there or symlink.
