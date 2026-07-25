# PRD: `squawk` — Voice-to-Terminal Dictation for macOS

**Status:** Draft
**Repo:** `squawk`

---

## 1. Problem statement

Typing long prompts and commands into terminal-based AI coding agents (Claude
Code, OpenCode, Codex CLI) and into the shell itself is slow. There is no
built-in way to speak into a terminal and have the words appear at the cursor.

**Goal:** press a key in iTerm, speak, and have the transcribed text inserted at
the current input position — whether that input is a zsh prompt or the composer
of a full-screen TUI agent (Claude Code / OpenCode / Codex).

## 2. Speech backend

squawk is backend-agnostic. It talks to any **OpenAI-compatible speech API**
over one base URL, using the standard transcription contract:

- Transcription: `POST {SQUAWK_SPEECH_URL}/audio/transcriptions`,
  `multipart/form-data` with fields `file` (audio) and `model`; response
  `{"text": "..."}`.
- Optional auth: `Authorization: Bearer {SQUAWK_SPEECH_KEY}` when a key is set.
- Health check: `GET {SQUAWK_SPEECH_URL}/models` with the key, asserting the
  configured model id is listed. Advisory only — many local servers omit or
  differently shape `/models`, so a surprising answer is a warning and the
  transcription round-trip is authoritative.

The endpoint can be a local server on your machine, a service on your LAN, or a
hosted provider — squawk neither knows nor cares, so long as it speaks the
OpenAI transcription contract. The default is OpenAI Whisper
(`https://api.openai.com/v1`, `whisper-1`), so a key alone is enough to start;
`squawk setup` records any other choice (see FR4).

### Local machine assumptions

- macOS 13+ with a working microphone.
- iTerm2 (3.6+) — supports **key-binding → "Run Coprocess…"**, whose stdout is
  injected into the session as if typed (works inside TUIs).
- `sox` (Homebrew) for audio capture; Python 3 (stdlib only).

## 3. Goals

1. One keystroke (or hold-to-talk) in iTerm starts recording; speech is
   transcribed by the configured endpoint and inserted at the cursor of whatever
   is running in the active pane — shell prompt, Claude Code, OpenCode, or Codex
   composer.
2. Low end-to-end latency after speech ends for a typical 5–15 s utterance
   (dominated by capture-stop + upload + backend transcription time).
3. Audio stays on-device except for the request to the endpoint the user chose.
4. Installable as a small CLI + iTerm keybinding (or the push-to-talk helper);
   no Accessibility-permission daemon required.

### Non-goals (v1)

- Continuous/always-on dictation or wake words.
- TTS (reading agent output aloud).
- Non-English transcription (depends on the backend model).
- Windows/Linux, Terminal.app, VS Code terminals.

## 4. Users & primary flow

Single-developer, macOS + iTerm. Canonical flow:

1. Focus an iTerm pane where Claude Code (or zsh, etc.) is waiting for input.
2. Trigger dictation (hold-to-talk on Space, or a one-shot hotkey).
3. An audible cue confirms recording started. User speaks.
4. Recording stops on **1.5 s of trailing silence** (auto, sox VAD) for the
   one-shot path, or instantly on release for hold-to-talk.
5. Transcript text is injected at the cursor, *not* submitted — the user reviews
   and presses Enter themselves.

## 5. Functional requirements

### FR1 — `squawk` CLI (core)

- `squawk dictate` — record → transcribe → print transcript to stdout (nothing
  else on stdout; logs → stderr). This is the coprocess mode.
  - `--keep-audio` — do not delete the captured WAV.
  - `--keep-newlines` — do not collapse internal newlines to spaces.
- `squawk ptt-start` / `squawk ptt-stop` — the two halves of push-to-talk.
- `squawk setup` — interactively choose a backend, persist it to
  `~/.config/squawk/config`, and verify it (see FR4).
- `squawk check` — print the resolved backend, then verify mic capture and
  speech-endpoint health with latency; failures are reported in backend terms.
- Audio capture: `sox` — 16 kHz, mono, 16-bit. Silence-based auto-stop for
  `dictate` (`silence 1 0.1 2% 1 1.5 2%`, tunable via env). Hard cap 60 s / 2 MB
  per utterance.
- Transcription: `POST {SQUAWK_SPEECH_URL}/audio/transcriptions`, multipart
  `file`/`model`, optional bearer auth. Timeouts: 5 s connect, 30 s total. Empty
  transcript: emit nothing, exit 0, "heard nothing" cue.
- Output hygiene: strip surrounding whitespace and any trailing newline (a stray
  `\n` would submit a TUI composer prematurely — a correctness requirement).
  Collapse internal newlines to spaces by default.

### FR2 — iTerm integration

- iTerm key binding **"Run Coprocess…"** → `~/bin/squawk dictate 2>/dev/null`.
  Coprocess stdout is fed to the session as keyboard input — no Accessibility API
  needed, works in full-screen TUIs.
- Push-to-talk delivers asynchronously via iTerm's scripting `write` verb
  (Automation permission), so it also lands in full-screen TUIs.
- Only one coprocess per session; starting a second recording fails gracefully.

### FR3 — Feedback & state

- Audible cue on record start, success, "heard nothing", and error. The terminal
  is visually busy (a TUI owns the screen), so audio is the primary channel.
- Lock file in `$TMPDIR` prevents concurrent recordings and hands the in-progress
  recording's PID/WAV from `ptt-start` to `ptt-stop`.

### FR4 — Configuration

Settings resolve as **environment variable → `KEY=value` config file at
`~/.config/squawk/config` → built-in default**:

```
SQUAWK_SPEECH_URL=https://api.openai.com/v1  # OpenAI-compatible speech base URL
SQUAWK_SPEECH_KEY     # bearer key for the endpoint (empty for keyless local servers)
SQUAWK_STT_MODEL=whisper-1   # transcription model id
SQUAWK_SILENCE_STOP=1.5   # seconds of trailing silence that stops `dictate`
SQUAWK_MAX_SECONDS=60     # hard cap on recording duration
SQUAWK_INPUT_DEVICE=default  # passed as AUDIODEV to sox when not "default"
```

The config file is line-oriented `KEY=value` (blank lines and `#` comments
ignored, quotes/whitespace stripped), holding any of the above; a legacy file
containing only a bare key is still read as `SQUAWK_SPEECH_KEY`. `squawk setup`
writes it (mode 600) and squawk warns if it finds looser permissions.

`squawk setup` is the guided path: pick OpenAI / Groq / a custom URL, enter an
optional key and model, and it persists the choice and verifies it against the
endpoint before returning. The default backend is OpenAI Whisper, so with only a
key configured squawk works with no further setup.

### FR5 — Error handling

- Endpoint unreachable/timeout: error cue + one-line message on **stderr** (never
  stdout in coprocess mode), exit non-zero. Nothing injected into the session.
- Mic permission denied (TCC): detect empty/failed capture, print remediation
  hint (grant microphone access in System Settings → Privacy & Security).
- Every failure degrades to "nothing injected" — squawk never types partial or
  error text into your session.

## 6. Architecture

```
iTerm trigger ──▶ squawk dictate  (or ptt-start / ptt-stop)
                    │
                    ├─ sox: mic → 16k mono WAV, silence auto-stop (or hold length)
                    ├─ POST /audio/transcriptions ──▶ configured OpenAI-compatible endpoint
                    └─ stdout / iTerm write ──▶ Claude Code / OpenCode / Codex / zsh
```

**Implementation:** a single Python script (stdlib only), using `subprocess` for
sox and `http.client` for the upload — no heavy deps, trivially editable. A small
signed helper app (`SquawkPTT.app`) owns the TCC microphone grant for the
push-to-talk path (see §8).

## 7. Phases

| Phase | Scope |
|---|---|
| **P1 — MVP** | `squawk dictate` + `squawk check`, sox capture with silence stop, iTerm coprocess keybinding, sounds |
| **P2 — Push-to-talk** | hold-to-talk trigger, instant stop on release, async delivery into the originating session, clipboard fallback |
| **P3 — Distribution** | configurable backend + `setup` flow, native trigger, packaging |

## 8. Security & privacy

- Audio is held only in `$TMPDIR` and deleted after transcription (`--keep-audio`
  debug flag to retain).
- The speech key is read from `~/.config/squawk/config` (chmod 600) or
  `SQUAWK_SPEECH_KEY`; it is never hardcoded or committed.
- squawk sends audio only to the endpoint the user configured — nowhere else.

### Microphone attribution (TCC)

macOS attributes microphone access to the *responsible process* at the top of
the spawn chain. Two lessons are baked into the design:

- A bare (non-bundle) binary can never be granted the microphone — requests from
  its children hang in "not determined" forever with no prompt. The push-to-talk
  path therefore routes through `SquawkPTT.app`, a signed app bundle that
  requests mic access on its own behalf; the recorder inherits the grant as its
  child.
- A background agent touching a **removable volume** blocks in `open()` on a
  consent it can't show, which is why the installer copies the CLI to
  `~/bin/squawk` instead of symlinking it from the repo.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| sox silence detection mis-tuned (cuts off speech or never stops) | Tunable thresholds; hard 60 s cap; hold-to-talk as manual override |
| iTerm coprocess quirks (stdin piping, one-per-session) | Ignore stdin; lock file |
| Trailing newline auto-submits agent composer | Explicit strip requirement (FR1); test against all three TUIs |
| Configured endpoint unreachable | Fast fail (5 s connect timeout) + error cue; terminal unaffected |
