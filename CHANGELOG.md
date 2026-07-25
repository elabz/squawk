# Changelog

## v0.1.2 — 2026-07-25

**Fixes push-to-talk being completely dead on a Homebrew install.** If you
installed with `brew install elabz/tap/squawk`, upgrade and re-run
`squawk install-agent`.

- The helper hardcoded `$HOME/bin/squawk`, which a Homebrew install never
  creates — the CLI lives in the keg, linked as `/opt/homebrew/bin/squawk`. The
  agent loaded, permissions granted, and `squawk doctor` went green, but holding
  Space did nothing: the hold was swallowed and the spawn failed with `ENOENT`.
- `squawk install-agent` now records the absolute path of the CLI that ran it
  into the LaunchAgent as `SQUAWK_CLI`, and prints it on success.
- The helper resolves the CLI (`SQUAWK_CLI` → `~/bin` → `/opt/homebrew/bin` →
  `/usr/local/bin`), logs which one it chose, and says so plainly when none is
  usable instead of failing silently on every hold.
- **`squawk doctor` gained a "CLI reachable by helper" check** — its absence is
  why this shipped, since every other check passes while push-to-talk is broken.

## v0.1.1 — 2026-07-25

- README: document `squawk doctor`, `squawk install-agent`, and
  `squawk uninstall` in the Usage section. All three shipped in v0.1.0 but were
  never listed there.
- Correct the claim that the helper's stable ad-hoc signing identifier keeps TCC
  grants across upgrades. macOS keys an ad-hoc app's grants to its **CDHash**,
  which tracks the compiled bytes — and Homebrew's build differs from a plain
  `clang` build of identical source, so moving between install methods
  re-prompts for Microphone / Accessibility / Input Monitoring once. Routine
  upgrades re-prompt only when the helper source actually changed.
- Homebrew formula: fix the `depends_on` ordering flagged by
  `brew audit --strict`.

## v0.1.0 — 2026-07-25

First public release. Everything below shipped in it.

### Homebrew distribution, `install-agent` / `uninstall` / `doctor`

- **One-command install** via a Homebrew tap: `brew install elabz/tap/squawk`.
  The formula compiles `SquawkPTT.app` **from source during install** — locally
  built binaries carry no Gatekeeper quarantine, so no Apple notarization,
  Developer account, or signing certificate is needed. A `curl … | sh` installer
  (`install.sh`) mirrors the same build for machines without Homebrew.
- **`squawk install-agent`** places `SquawkPTT.app` in `~/Applications`, installs
  and loads its LaunchAgent, and confirms it is running (using a prebuilt app
  when present, else building from source). **`squawk uninstall`** unloads the
  agent, removes the app and LaunchAgent plist, and lists the TCC entries to
  revoke. The formula delegates these stateful, permission-bound steps to the CLI.
- **`squawk doctor`** — one-stop preflight: checks `sox`, the build toolchain,
  the LaunchAgent, SquawkPTT's Microphone/Accessibility/Input Monitoring grants
  (read from its own log — the only reliable way to see another process's TCC
  state), Automation for iTerm, and the speech backend. It prints the exact
  System Settings pane for each failing item, warns about a lingering Karabiner
  rule, and exits non-zero if any required check fails.
- The helper build (`clang` compile + ad-hoc `codesign --identifier sh.squawk.ptt`)
  is factored into `helper/build.sh`, shared by the formula, the curl installer,
  `helper/install.sh`, and `squawk install-agent`.

### native hold-detect (Karabiner removed)

- **No more Karabiner-Elements.** SquawkPTT now detects tap-vs-hold on Space
  itself via a `CGEventTap`, replacing the Karabiner rule, the
  `~/.local/state/squawk/ptt` trigger-file protocol, and the helper's poll loop.
  The cask, its virtual-HID driver, and the per-command consent dialog are no
  longer prerequisites. Hold-to-talk behavior is unchanged (hold Space in iTerm
  to talk, tap for a normal space; threshold `SQUAWK_HOLD_MS`, default 400 ms).
- **New permissions**: SquawkPTT now needs **Accessibility** (to alter/synthesize
  Space) and **Input Monitoring** (to observe keys), in addition to Microphone.
  Until Accessibility is granted the tap stays down and the keyboard behaves
  normally; the helper logs guidance and self-arms once granted — no restart.
- **Privacy hardening**: the helper log moved from world-readable
  `/tmp/squawkptt.log` to the per-user-private `~/Library/Logs/squawkptt.log`,
  and a delivery-failure log line no longer records the transcript text (only its
  length). No transcript or audio is written to disk in normal operation.
- **`squawk migrate`**: one-time, reversible crossover for Karabiner-based
  (vox-era) installs. Backs up `karabiner.json`, removes the legacy hold-Space
  rule, unloads the `org.elabz.voxptt` agent, and prints exact rollback steps.
  The native tap refuses to arm while it still detects the legacy rule, so Space
  is never grabbed by two owners at once — run `squawk migrate` first.

Migrating from the previous (Karabiner-based) build: run `~/bin/squawk migrate`,
then rebuild the helper with `helper/install.sh` and grant Accessibility + Input
Monitoring to SquawkPTT when prompted. Karabiner-Elements can be uninstalled if
it was installed only for squawk. Rollback = the steps `migrate` prints.

### configurable backend, `squawk setup`

- **Default backend**: OpenAI Whisper (`https://api.openai.com/v1`, `whisper-1`).
  With only an API key configured, squawk works with no further setup.
- **`squawk setup`**: interactive onboarding — pick OpenAI, Groq, or a custom
  OpenAI-compatible URL, enter an optional key and model, write
  `~/.config/squawk/config` (mode 600), and verify against the endpoint
  immediately. Re-runnable; detects a legacy bare-key config file and offers to
  wrap it in the new format.
- **Config file** is now `KEY=value`, holding any `SQUAWK_*` setting rather than
  just the key. Precedence is environment variable → config file → built-in
  default. A bare-key file is still read as the key, so existing installs keep
  working. squawk warns on stderr if the file's mode is looser than 600.
- **Keyless local servers** are supported: with no key configured, no
  `Authorization` header is sent (whisper.cpp `server` and friends expect this).
- **`squawk check`** prints the resolved backend URL, model, and whether a key
  resolved, and translates failures into backend-specific guidance (key rejected,
  model not served, local server not running). A missing or unhelpful `/models`
  endpoint is now a `WARN`, not a failure — the transcription round-trip decides.

Migrating from the previous build: nothing is required. Your existing bare-key
config file still works against the default OpenAI backend; run `squawk setup` to
record a different URL/model in the new format.

### rename to `squawk`

The project was renamed from its internal name to **squawk** and scrubbed of all
private, environment-specific identifiers so it can be released as open source.
Behavior is unchanged; only names and paths moved.

### Migrating an existing install (from the pre-rename `vox` build)

This is a one-time migration for the single existing local install. Every name,
path, and identity changed, so:

1. **Move your key/config file:**
   ```bash
   mkdir -p ~/.config/squawk && chmod 700 ~/.config/squawk
   mv ~/.config/vox/key ~/.config/squawk/config 2>/dev/null || true
   chmod 600 ~/.config/squawk/config
   rmdir ~/.config/vox 2>/dev/null || true
   ```
2. **Rename any environment variables** you set: `VOX_SPEECH_URL` →
   `SQUAWK_SPEECH_URL`, `VOX_SPEECH_KEY` → `SQUAWK_SPEECH_KEY`, `VOX_STT_MODEL` →
   `SQUAWK_STT_MODEL`, `VOX_SILENCE_STOP` → `SQUAWK_SILENCE_STOP`,
   `VOX_MAX_SECONDS` → `SQUAWK_MAX_SECONDS`, `VOX_INPUT_DEVICE` →
   `SQUAWK_INPUT_DEVICE`. (Your LAN endpoint is no longer a built-in default —
   record it with `squawk setup` → `3) Custom / local`, or set
   `SQUAWK_SPEECH_URL` and `SQUAWK_STT_MODEL` explicitly.)
3. **Remove the old helper app and LaunchAgent:**
   ```bash
   launchctl bootout "gui/$(id -u)/org.elabz.voxptt" 2>/dev/null || true
   rm -f ~/Library/LaunchAgents/org.elabz.voxptt.plist
   rm -rf ~/Applications/VoxPTT.app
   ```
4. **Reinstall the renamed helper** (builds `SquawkPTT.app`, loads the
   `sh.squawk.ptt` agent, copies `~/bin/squawk`):
   ```bash
   helper/install.sh
   ```
5. **Re-grant Microphone** to **SquawkPTT** on first run — TCC grants attach to
   the code identity, and the bundle id changed, so macOS treats it as a new app.
   Also re-approve **Automation** (control iTerm) the first time a transcript is
   delivered.
6. If you were on the old Karabiner-based trigger, run `~/bin/squawk migrate` to
   remove its hold-Space rule and the legacy agent — the native event tap in the
   rebuilt helper now handles hold-detect (see the native hold-detect entry
   above). Karabiner-Elements is no longer required.

Verify with `~/bin/squawk check`. Rollback = reinstate the old tree from git
history.
