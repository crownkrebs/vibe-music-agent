# Security Policy

## Supported versions

This is a personal-tool project. Only the `master` branch is actively
maintained.

| Version | Supported |
|---------|-----------|
| `master` | ✅ |
| Older commits / forks | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for security reports.**

Instead, use GitHub's private vulnerability disclosure flow:

1. Go to https://github.com/crownkrebs/vibe-music-agent/security
2. Click **Report a vulnerability**
3. Fill out the advisory form with:
   - A description of the issue
   - Reproduction steps (or a proof of concept)
   - Affected files / commit hash
   - The impact you believe it has

You'll get an acknowledgment within a few days. Confirmed issues will be fixed
on `master` and a security advisory will be published once a patch ships.

## In scope

- Credential handling — anything that could leak `config.json`, OAuth tokens,
  or LLM API keys
- Dependency vulnerabilities affecting Flask, Spotipy, the Anthropic / OpenAI
  SDKs, or transitive deps in `requirements.txt`
- Prompt-injection vectors that could trick the agent into adding tracks to
  unintended playlists, deleting data, or exfiltrating local state
- Path traversal or arbitrary file write through `/api/config`,
  `/api/taste/analyze`, or any other route
- Authentication bypass (the local-only Flask binding is the auth boundary)

## Out of scope

- Denial of service against your own local Flask server (you control it; just
  restart it)
- Social-engineering attacks against Anthropic, OpenAI, or Spotify
- Vulnerabilities in third-party services we depend on but do not maintain
  (report those directly to the upstream project)
- Issues that require an attacker to already have unrestricted local access
  to the user's machine

## Hardening recommendations

Even though this app binds to `127.0.0.1` only:

- **Never commit `config.json`** — it's gitignored by default; keep it that way
- **Rotate credentials** if you suspect leakage. Anthropic, OpenAI, and Spotify
  all support key rotation from their respective dashboards
- **Don't expose `localhost:5000` over a tunnel** (e.g. ngrok) without adding
  authentication first — the API has no auth layer
- **Audit `TASTE_PROFILE.md` before sharing** if you ever publish it; it
  contains a portrait of your listening history

## Acknowledgments

Security researchers who report valid issues will be credited in the security
advisory unless they prefer to remain anonymous.
