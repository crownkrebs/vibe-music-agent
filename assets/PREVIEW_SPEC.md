# Preview asset specification

This file describes the visual assets that should live in `assets/` for use
in the README and other documentation. We don't ship binary screenshots in
this repo by default — contributors who want to add them can drop `.png` /
`.gif` files into this directory, then reference them from the README.

## Hero screenshot — `hero.png`

**What it shows:** the main chat UI in a fresh state, with a partially typed
vibe in the input ("late-night drive in the rain") and the previous turn's
verified tracks visible above as a list with album art thumbnails.

**Composition:**
- ~1600×900 px, 16:9 ratio
- Dark theme (matches the app's default)
- Visible: the chat input, one completed turn with verified tracks, the
  playlist target chip ("🌙 Late Night"), and the status line ("✓ verified
  30 tracks · added")

## First-run banner — `first-run.png`

**What it shows:** the "Analyze My Taste" banner that appears on first
launch, before any taste profile has been generated.

**Composition:**
- ~1200×400 px
- Top of the app, banner spanning full width
- Caption text: "First time here? Analyze your taste so the agent learns
  who you are."
- Single CTA button: "Analyze My Taste"

## Verification animation — `verification.gif`

**What it shows:** a short loop (~6s) of the verification pipeline running.
Candidate track names appear, then get checkmarks (verified) or X marks
(rejected — token mismatch), then the final ordered list appears.

**Composition:**
- ~800×600 px
- Captures the moment that demonstrates the zero-hallucination guarantee
  visually

## Architecture diagram — `architecture.png`

**What it shows:** a clean rendered version of the system architecture
described in `docs/ARCHITECTURE.md`. Use the Mermaid source as the canonical
spec; this PNG is just a higher-resolution / branded export.

**Composition:**
- ~1600×900 px, transparent background
- Same labels, same arrows as the Mermaid version
- Brand colors: muted accent on the LLM and Spotify Verify nodes

## Conventions

- Place all assets in `assets/`
- Use lowercase, hyphenated filenames
- Reference from the README with relative paths: `![alt](assets/hero.png)`
- Keep individual images under 1 MB; optimize PNGs with `oxipng` or similar
- Don't commit assets that contain real account data (real playlist names,
  real track titles from a personal library) — use a throwaway Spotify
  account for screenshots
