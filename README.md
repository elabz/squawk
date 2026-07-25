# squawk — voice-to-terminal dictation

Press a key in iTerm, speak, and the transcript lands at the cursor — in a zsh
prompt or inside a full-screen TUI composer (Claude Code, OpenCode, Codex).
squawk records your microphone, sends the audio to an **OpenAI-compatible speech
endpoint you choose**, and types the transcript back into the terminal. Audio
goes only to that endpoint — nowhere else.

See [`docs/PRD.md`](docs/PRD.md) for the full design.

## Quickstart

```bash
brew install elabz/tap/squawk
squawk install-agent   # build SquawkPTT.app + load the push-to-talk agent
squawk setup           # choose your speech backend
squawk doctor          # verify permissions + backend, with fixes for anything missing
```

The formula compiles the helper **from source on your machine**, so there's no
Gatekeeper block and no Apple notarization/Developer account involved. Grant the
permissions `squawk doctor` lists, and hold Space in iTerm to talk.

No Homebrew? Use the installer script instead (read it first):

```bash
curl -fsSL https://raw.githubusercontent.com/elabz/squawk/main/install.sh | sh
squawk install-agent && squawk setup && squawk doctor
```

## Prerequisites

- macOS 13+ with iTerm2 (3.6+ recommended).
- `sox` installed (`brew install sox`).
- Python 3 (stdlib only, no pip deps).
- A speech-to-text backend: an OpenAI API key works out of the box, or point
  squawk at a local/LAN OpenAI-compatible server. `squawk setup` walks you
  through either — see [Backends](#backends).

## Install

The [Quickstart](#quickstart) `brew`/`curl` paths install everything. To install
from a checkout without either — just the CLI, so you can run `install-agent`
yourself:

```bash
mkdir -p ~/bin
cp squawk.py ~/bin/squawk
chmod +x ~/bin/squawk
squawk install-agent   # builds SquawkPTT.app from helper/ and loads the agent
```

`~/bin` only needs to be on your `$PATH` for typing `squawk setup` / `squawk check` manually at a
shell prompt — the push-to-talk helper and iTerm binding invoke `~/bin/squawk`
by absolute path. To get the manual command too, add to `.zshrc`:

```bash
export PATH="$HOME/bin:$PATH"
```

`squawk install-agent` places `SquawkPTT.app` in `~/Applications` and loads its
LaunchAgent; `squawk uninstall` reverses it; `squawk doctor` checks everything.

## Configure

```bash
squawk setup
```

That's the whole thing: pick a backend, paste a key (or leave it blank for a
keyless local server), and `setup` writes `~/.config/squawk/config` (mode 600)
and immediately verifies it against the endpoint — so a bad key surfaces now
rather than mid-sentence. It is idempotent; re-run it any time to switch
backends.

The config file is the durable store rather than your shell profile because
squawk runs from the push-to-talk helper (and, on the fallback path, an iTerm
coprocess) with a bare environment that never sources `~/.zshrc` — an exported
`SQUAWK_SPEECH_KEY` is invisible on the paths that matter.

Each setting resolves as **environment variable → config file → built-in
default**, so a one-off `SQUAWK_STT_MODEL=… squawk check` still overrides the
file:

| Variable | Default | Purpose |
|---|---|---|
| `SQUAWK_SPEECH_URL` | `https://api.openai.com/v1` | OpenAI-compatible speech base URL |
| `SQUAWK_SPEECH_KEY` | *(none — many local servers need none)* | Bearer key for the endpoint |
| `SQUAWK_STT_MODEL` | `whisper-1` | Model id for transcription |
| `SQUAWK_SILENCE_STOP` | `1.5` | Seconds of trailing silence that stops recording |
| `SQUAWK_MAX_SECONDS` | `60` | Hard cap on recording duration |
| `SQUAWK_INPUT_DEVICE` | `default` | Passed as `AUDIODEV` to sox when not `default` |

The file is plain `KEY=value`, one per line (`#` comments allowed), so you can
hand-edit it instead of re-running `setup`:

```
SQUAWK_SPEECH_URL=http://127.0.0.1:8080/v1
SQUAWK_SPEECH_KEY=
SQUAWK_STT_MODEL=whisper-1
SQUAWK_SILENCE_STOP=1.2
```

It holds your API key, so keep it at mode 600 — squawk warns on stderr if it
finds anything looser.

## Backends

squawk speaks one protocol: the OpenAI audio API (`POST
/audio/transcriptions`, multipart `file` + `model`). Anything that implements it
works, hosted or local — only the URL, key, and model id differ.

**OpenAI** (the default). Create a key at
[platform.openai.com/api-keys](https://platform.openai.com/api-keys), then:

```bash
squawk setup    # choose 1) OpenAI, paste the key, accept whisper-1
```

Billing note: OpenAI charges for Whisper per minute of audio (~$0.006/min at the
time of writing — check [current pricing](https://openai.com/api/pricing/)).
Dictation is short bursts, so this is pennies a day, but it is not free and your
audio leaves the machine. The local options below cost nothing and don't.

**Groq** — same API, faster and cheaper, `whisper-large-v3`. Get a key at
[console.groq.com/keys](https://console.groq.com/keys) and choose `2) Groq` in
`squawk setup`.

**Anything else** — choose `3) Custom / local` and enter the base URL and model
id. This is also the path for a LAN gateway (LiteLLM, LocalAI, a reverse proxy)
that fronts your own models.

## Local / private STT

With a local server, **audio never leaves the machine** — no key, no network, no
per-minute billing. Each recipe below is a server on one side and the answers to
`squawk setup` on the other (choose `3) Custom / local`, then leave the API key
prompt **blank** — squawk sends no `Authorization` header when no key is set).

**whisper.cpp** — leanest option, no Docker, Metal-accelerated on Apple silicon.
Its server must be built from source: Homebrew's `whisper-cpp` bottle is
configured with `WHISPER_BUILD_SERVER=OFF` and ships only `whisper-cli`.

```bash
git clone https://github.com/ggml-org/whisper.cpp && cd whisper.cpp
sh ./models/download-ggml-model.sh large-v3-turbo   # ≈1.6 GB; or base.en ≈150 MB
cmake -B build -DWHISPER_BUILD_SERVER=ON && cmake --build build --config Release -j
./build/bin/whisper-server --model models/ggml-large-v3-turbo.bin \
  --host 127.0.0.1 --port 8080 \
  --request-path /v1 --inference-path /audio/transcriptions
```

The two path flags matter: `whisper-server` serves transcription at `/inference`
by default, and squawk (like every OpenAI client) posts to
`{base}/audio/transcriptions`. The flags above move its route to
`/v1/audio/transcriptions` so the two line up.

```
squawk setup → 3
  base URL: http://127.0.0.1:8080/v1
  model id: whisper-1        (whisper-server serves the model it was loaded with
                              and ignores this field — any value works)
  API key:  (blank)
```

`whisper-server` implements no `/models` endpoint, so `squawk check` prints
`[endpoint] WARN … relying on the round-trip below` and lets the actual
transcription be the verdict. That is expected, not a problem.

**faster-whisper via Speaches** — a fuller OpenAI-compatible server: real
`/models`, streaming, and on-demand model loading, in one container.

```bash
docker run --rm --detach --publish 8000:8000 --name speaches \
  --volume hf-hub-cache:/home/ubuntu/.cache/huggingface/hub \
  ghcr.io/speaches-ai/speaches:latest-cpu
```

```
squawk setup → 3
  base URL: http://127.0.0.1:8000/v1
  model id: Systran/faster-whisper-large-v3   (downloaded on first request)
  API key:  (blank)
```

**LocalAI** — worth it if you already run it: one server for both LLMs and
transcription, and its gallery ships a model registered under the OpenAI name
`whisper-1`.

```bash
docker run -ti --name local-ai -p 8080:8080 localai/localai:latest
# then install the whisper model once, from the gallery:
curl http://127.0.0.1:8080/models/apply -H 'Content-Type: application/json' \
  -d '{"id": "whisper-1"}'
```

```
squawk setup → 3
  base URL: http://127.0.0.1:8080/v1
  model id: whisper-1
  API key:  (blank)
```

Any of these can equally live on another machine on your LAN — point the base URL
at that host instead of `127.0.0.1` and the audio stays inside your network.

Not usable as a backend: **LM Studio** and **Ollama**. Both expose an
OpenAI-compatible API, but only for chat/completions, embeddings, and `/models` —
neither implements `/audio/transcriptions`, so there is nothing for squawk to talk
to. Use one of the three servers above.

## Trigger: hold Space in iTerm (push-to-talk)

The shipped trigger is **hold Space to talk** in iTerm, tap Space to type a
normal space. **No external key-remapper, no iTerm key bindings, and no coprocess
are involved** — the helper does everything itself, and nothing ever takes over
the terminal keyboard:

1. **SquawkPTT.app** (`helper/`, installed to `~/Applications`, kept alive by the
   `sh.squawk.ptt` LaunchAgent) places a `CGEventTap` and, **only while iTerm is
   frontmost**, tells a tap of Space apart from a hold: release within the
   threshold (default 400 ms, `SQUAWK_HOLD_MS`) → an ordinary space; hold past it
   → `~/bin/squawk ptt-start`, release → `~/bin/squawk ptt-stop`. Space behaves
   completely normally in every other app and for every other key. It also owns
   the TCC microphone grant: it is a real signed app bundle with
   `NSMicrophoneUsageDescription`, requests mic access itself at startup (the
   prompt macOS refuses to show for bare binaries), and sox inherits the grant as
   its child. Logs to `~/Library/Logs/squawkptt.log`.
2. **squawk** records exactly your physical hold (no silence-based cutoff) and
   delivers **asynchronously**: `ptt-stop` signals the recorder and returns
   immediately; a detached worker transcribes and types the text into iTerm's
   frontmost session via its scripting `write` verb (Automation permission) —
   this lands in full-screen TUIs too.

Install/update the helper. With squawk on `PATH` (brew or curl install) this is
just:

```bash
squawk install-agent
```

From a raw checkout, `helper/install.sh` does the same and also syncs
`~/bin/squawk`. Both build `SquawkPTT.app` via the shared `helper/build.sh`.

On first run macOS prompts once each for **Accessibility** (to alter/synthesize
Space) and **Input Monitoring** (to observe keys), plus **Microphone**. Until
Accessibility is granted the tap simply never comes up and the keyboard behaves
normally; the helper logs how to grant it and keeps retrying, so no restart is
needed. No Karabiner-Elements, no virtual-HID driver, no per-command consent.

> **Upgrading from a Karabiner-based (vox-era) install?** Run `~/bin/squawk
> migrate` first: it backs up `karabiner.json`, removes the old hold-Space rule,
> unloads the legacy `org.elabz.voxptt` agent, and prints exact rollback steps.
> The native tap refuses to arm while it still detects the old rule, so run
> `migrate` before relying on hold-to-talk. Karabiner-Elements can then be
> uninstalled if it was installed only for squawk
> (`brew uninstall --cask karabiner-elements`).

### Manual fallback: one-shot dictation hotkey

The one-shot `dictate` command records until trailing silence and prints the
transcript to stdout. Bound as an iTerm **Run Coprocess** global key mapping,
iTerm injects that stdout into the session exactly like typed keystrokes. Add it
**through iTerm's own UI** (don't hand-edit `com.googlecode.iterm2.plist` — its
internal action codes aren't stable): Settings → Profiles → Keys → Key Mappings →
Global → **+**, press your chosen key, Action **Run Coprocess...**, command
`~/bin/squawk dictate 2>/dev/null`.

Only one recording can be active at a time — squawk enforces this with a lock
file (also used to hand the in-progress recording's PID and WAV path from
`ptt-start` to `ptt-stop`, since they run as separate processes). A stray second
`ptt-start` while one is already active just no-ops rather than touching the mic
again.

## Permissions (TCC)

macOS attributes microphone and Automation access to the *responsible process* —
the app at the top of the spawn chain — so each path prompts for its own parent,
one time each. `squawk doctor` reports the live status of every item below and
prints the exact pane link for anything missing; the panes are also listed here.

- **Microphone / SquawkPTT** (push-to-talk): SquawkPTT requests it at launch —
  approve "SquawkPTT would like to access the microphone". Without it, sox
  records nothing and every utterance comes back empty.
  Pane: *System Settings → Privacy & Security → Microphone*.
- **Accessibility / SquawkPTT** (push-to-talk): required to alter Space and post
  the synthetic space for a tap. Without it the event tap never comes up — the
  keyboard keeps working normally and the helper logs how to grant it.
  Pane: *Privacy & Security → Accessibility*.
- **Input Monitoring / SquawkPTT** (push-to-talk): required to observe key
  events; prompted when the tap is created.
  Pane: *Privacy & Security → Input Monitoring*.
- **Microphone / iTerm** (`dictate`, `squawk check` run from a shell).
  Pane: *Privacy & Security → Microphone*.
- **Automation** (control iTerm2): prompted the first time the push-to-talk
  worker delivers a transcript. Grant it, or transcripts land on the
  clipboard/nowhere.
  Pane: *Privacy & Security → Automation → (the app squawk runs under) → iTerm*.

`squawk doctor` reads SquawkPTT's own log to report its Microphone /
Accessibility / Input Monitoring grants accurately (a process can't query
another app's TCC state directly), tests Automation by driving iTerm, and
flags a lingering Karabiner rule.

Two hard-won TCC rules baked into the design:

- A bare (non-bundle) binary can never be granted the microphone — requests from
  its children hang in "not determined" forever with no prompt and no Settings
  entry. That is why the push-to-talk path routes through a signed app bundle.
- A background agent touching a **removable volume** blocks in `open()` on a
  consent it can't show, which is why `install.sh` copies `squawk.py` to
  `~/bin/squawk` instead of symlinking it from the repo.

## Verify: `squawk check`

```bash
~/bin/squawk check
```

It echoes the resolved backend, then reports three things and exits non-zero if
any fail:

```
[backend]   https://api.openai.com/v1  model=whisper-1  key=set
[mic]       OK - mic OK
[endpoint]  OK - endpoint OK (serves whisper-1)
[roundtrip] OK - 0.39s (text: 'Beep')
```

- `[backend]` — the URL, model, and whether a key resolved, after applying
  env-over-file-over-default precedence. Check this first when squawk is talking
  to the wrong place.
- `[mic]` — records 0.5s and asserts it isn't pure silence. On failure, see the
  TCC remediation above.
- `[endpoint]` — `GET /models` against the resolved URL. `FAIL` means the server
  is unreachable or the key was rejected; `WARN` means `/models` was missing or
  didn't list your model, which many local servers do legitimately — the
  round-trip below then decides.
- `[roundtrip]` — transcribes a synthesized tone and reports latency. This is the
  authoritative test of the backend, but it measures connectivity and speed, not
  transcription accuracy (a sine tone isn't speech).

Failures are reported in terms of your backend: a rejected key, a model the
endpoint doesn't serve, or a local server that isn't running each get their own
message and remediation.

## Usage

- `squawk setup` — choose a backend, save it to `~/.config/squawk/config`, and
  verify it. Re-runnable; see [Configure](#configure).
- `squawk dictate` — record until silence, transcribe, print the sanitized
  transcript to stdout with no trailing newline (so it won't auto-submit an agent
  composer). Logs go to stderr only. Manual/fallback trigger.
  - `--keep-audio` — don't delete the captured WAV from `$TMPDIR`.
  - `--keep-newlines` — don't collapse internal newlines to spaces.
- `squawk ptt-start` / `squawk ptt-stop` — the two halves of push-to-talk (see
  above). Not meant to be run manually except for testing. Both return
  immediately: `ptt-start` leaves recording running in a detached background
  process; `ptt-stop` signals that recorder to stop and hands off to a detached
  worker that transcribes and types the result into the session that started the
  recording — same `--keep-audio`/`--keep-newlines` flags as `dictate`. If that
  session has since closed, the worker copies the transcript to the clipboard
  instead of typing it somewhere unexpected.
- `squawk check` — diagnostics, see above.
- `squawk migrate` — one-time crossover from a legacy Karabiner-based install:
  removes the old hold-Space rule (backing up `karabiner.json`), unloads the
  legacy `org.elabz.voxptt` agent, and prints exact rollback steps. Safe to run
  when there's nothing to migrate (it says so and changes nothing).

Audible feedback (since a full-screen TUI owns the display): a start chime when
recording begins, a success chime when text is emitted, a distinct "heard
nothing" chime for an empty transcript, and an error chime on any failure. Every
failure mode degrades to "nothing injected" — squawk never types partial or error
text into your session.

## Troubleshooting

Run `squawk check` first — it isolates whether the problem is the microphone, the
network path to your endpoint, or the key's authorization. Common cases:

- **Holding Space just repeats spaces / does nothing special**: the event tap
  isn't armed. Check `~/Library/Logs/squawkptt.log` — it says whether the tap is active,
  whether **Accessibility** was granted (grant it in System Settings → Privacy &
  Security → Accessibility, then it self-arms), and whether a **legacy Karabiner
  rule** was detected (run `~/bin/squawk migrate` to remove it — the tap refuses
  to arm while both could grab Space). Also confirm iTerm is actually frontmost;
  the tap only touches Space there.
- **Beep on every attempt, nothing typed**: check the detached worker's log at
  `$TMPDIR/squawk-worker.log` — it records each delivery attempt (transcription
  errors, injection target, character count). For the non-ptt paths, run
  `~/bin/squawk dictate` directly in a terminal (not as a coprocess) to see
  stderr.
- **Success chime but nothing typed (push-to-talk only)**: the detached worker
  couldn't drive iTerm. Confirm **System Settings → Privacy & Security →
  Automation** grants iTerm permission to control iTerm; if the transcript keeps
  landing on the clipboard, the originating session is being closed before the
  worker finishes.
- **"heard nothing" beep every time you do speak**: for `dictate`, try
  `SQUAWK_SILENCE_STOP` lower/higher; for push-to-talk, make sure you're holding
  Space for the entire utterance (release stops the recording immediately).
  Confirm mic input level with `squawk check`.

## License

MIT — see [`LICENSE`](LICENSE).
