# Contributing to squawk

Thanks for your interest in squawk. It is a small, single-file Python CLI plus a
tiny macOS helper app — deliberately dependency-free and easy to read.

## Project layout

- `squawk.py` — the CLI (stdlib only). Installed as `~/bin/squawk`.
- `helper/` — the push-to-talk helper:
  - `squawkptt.m` — the resident app that owns the TCC microphone grant.
  - `Info.plist`, `sh.squawk.ptt.plist` — app bundle + LaunchAgent metadata.
  - `install.sh` — builds `SquawkPTT.app`, installs it, loads the LaunchAgent.
- `docs/` — product requirements and design notes.

## Development

- Keep `squawk.py` on the Python standard library — no third-party runtime
  dependencies. The fast cold-start matters because it runs per keystroke.
- squawk is backend-agnostic: it speaks the OpenAI-compatible transcription
  contract (`POST /audio/transcriptions`) and must not hardcode any specific
  endpoint, model, or key.
- macOS-specific behavior (TCC attribution, iTerm scripting) is documented inline
  where it is non-obvious — please keep those comments accurate if you change the
  surrounding code.

## Testing a change

- `squawk check` — verifies mic capture and endpoint health end to end.
- `helper/install.sh` — rebuild and reload the helper after touching anything in
  `helper/`.

## Pull requests

- Keep changes focused and explain the "why", especially for anything touching
  the recording lifecycle or macOS permissions.
- Do not commit secrets, API keys, or machine-specific paths. Backends are
  configured by the user, never baked in.

By contributing, you agree that your contributions are licensed under the MIT
License (see `LICENSE`).
