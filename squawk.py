#!/usr/bin/env python3
"""squawk — voice-to-terminal dictation. Record mic audio, transcribe via an
OpenAI-compatible speech endpoint, and print the transcript to stdout."""

import argparse
import base64
import getpass
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
import wave
from dataclasses import dataclass

# The default backend is OpenAI's transcription API: with only a key configured,
# squawk works out of the box. Any OpenAI-compatible endpoint (Groq, whisper.cpp
# `server`, faster-whisper, LM Studio, a LAN gateway) is selected by overriding
# SQUAWK_SPEECH_URL / SQUAWK_STT_MODEL — see `squawk setup`.
DEFAULT_SPEECH_URL = "https://api.openai.com/v1"
DEFAULT_STT_MODEL = "whisper-1"
DEFAULT_SILENCE_STOP = "1.5"
DEFAULT_MAX_SECONDS = "60"
DEFAULT_INPUT_DEVICE = "default"

# Hosts that will never accept an anonymous request, so a missing key is worth
# reporting up front instead of as a puzzling 401. Local/self-hosted servers are
# deliberately absent: they are often keyless and must stay that way.
KEY_REQUIRED_HOSTS = frozenset({"api.openai.com", "api.groq.com"})

CONNECT_TIMEOUT = 5
TOTAL_TIMEOUT = 30
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
WAV_HEADER_BYTES = 44

SOUND_START = "/System/Library/Sounds/Pop.aiff"
SOUND_ACK = "/System/Library/Sounds/Ping.aiff"  # played the instant Space is released
SOUND_SUCCESS = "/System/Library/Sounds/Glass.aiff"
SOUND_EMPTY = "/System/Library/Sounds/Tink.aiff"
SOUND_ERROR = "/System/Library/Sounds/Basso.aiff"

# Visual mode indicator: the same states the sounds above signal, rendered as a
# glyph in the iTerm session so "mic is hot" and "still thinking" are visible and
# not just audible. Each state maps to (glyph, cursor colour); a None colour
# leaves the cursor at the profile default.
#
# iTerm draws the badge DESATURATED — it applies the badge colour and alpha to
# the glyph, so emoji lose their hue. States must therefore be distinguishable by
# SHAPE; colour carries no information and is only an accent on the text cursor.
INDICATOR_GLYPHS = {
    "recording": "🎤",
    "transcribing": "💭",
    "success": "✅",
    "empty": "🔇",
    "error": "❗",
}
INDICATOR_COLORS = {
    "recording": "#FF3B30",     # red — mic is live
    "transcribing": "#FF9F0A",  # amber — waiting on the transcription
}
INDICATOR_STATES = tuple(INDICATOR_GLYPHS)
# Terminal states linger just long enough to be noticed, then clear. Only the
# detached worker shows these, and it has already delivered the transcript by
# then, so the wait costs nothing the user is waiting on.
INDICATOR_FLASH_SECONDS = 1.0
# Surfaces: "badge" is the session badge, "cursor" the text-cursor colour,
# "both" is the default, "off" disables the visual channel entirely.
INDICATOR_MODES = ("both", "badge", "cursor", "off")
DEFAULT_INDICATOR = "both"

LOCK_PATH = os.path.join(tempfile.gettempdir(), "squawk.lock")
WORKER_LOG = os.path.join(tempfile.gettempdir(), "squawk-worker.log")
SOX_LOG = os.path.join(tempfile.gettempdir(), "squawk-sox.log")


def find_sox():
    # squawk runs from a hold-to-talk trigger (or an iTerm coprocess) with a
    # minimal PATH that lacks the Homebrew prefix, so a bare "sox" raises
    # FileNotFoundError there even though it works from an interactive shell.
    # Resolve to an absolute path.
    found = shutil.which("sox")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/sox", "/usr/local/bin/sox"):
        if os.access(candidate, os.X_OK):
            return candidate
    return "sox"


SOX = find_sox()


@dataclass
class Config:
    speech_url: str
    speech_key: str
    stt_model: str
    silence_stop: float
    max_seconds: float
    input_device: str
    indicator: str
    indicator_glyphs: dict
    indicator_colors: dict


CONFIG_DIR = os.path.expanduser("~/.config/squawk")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config")


def _glyph_key(state):
    return f"SQUAWK_INDICATOR_GLYPH_{state.upper()}"


def _color_key(state):
    return f"SQUAWK_INDICATOR_COLOR_{state.upper()}"


# Per-state glyph and cursor-colour overrides are generated rather than spelled
# out, so adding a state to INDICATOR_GLYPHS makes it configurable for free.
INDICATOR_SETTING_KEYS = tuple(
    [_glyph_key(s) for s in INDICATOR_STATES] + [_color_key(s) for s in INDICATOR_STATES]
)

SETTING_KEYS = (
    "SQUAWK_SPEECH_URL",
    "SQUAWK_SPEECH_KEY",
    "SQUAWK_STT_MODEL",
    "SQUAWK_SILENCE_STOP",
    "SQUAWK_MAX_SECONDS",
    "SQUAWK_INPUT_DEVICE",
    "SQUAWK_INDICATOR",
) + INDICATOR_SETTING_KEYS

DEFAULTS = {
    "SQUAWK_SPEECH_URL": DEFAULT_SPEECH_URL,
    "SQUAWK_SPEECH_KEY": "",
    "SQUAWK_STT_MODEL": DEFAULT_STT_MODEL,
    "SQUAWK_SILENCE_STOP": DEFAULT_SILENCE_STOP,
    "SQUAWK_MAX_SECONDS": DEFAULT_MAX_SECONDS,
    "SQUAWK_INPUT_DEVICE": DEFAULT_INPUT_DEVICE,
    "SQUAWK_INDICATOR": DEFAULT_INDICATOR,
}
DEFAULTS.update({_glyph_key(s): g for s, g in INDICATOR_GLYPHS.items()})
DEFAULTS.update({_color_key(s): INDICATOR_COLORS.get(s, "") for s in INDICATOR_STATES})


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def parse_config_text(text):
    """Parse the line-oriented `KEY=value` config format. Blank lines and `#`
    comments are ignored; surrounding whitespace and matching quotes are stripped.
    A file with no `=` at all is the legacy bare-key format (the whole file was
    the speech key), so it is read as SQUAWK_SPEECH_KEY to keep older installs
    working — `squawk setup` offers to rewrite it."""
    settings = {}
    bare_lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            bare_lines.append(line)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key:
            settings[key] = unquote(value)
    if not settings and len(bare_lines) == 1:
        settings["SQUAWK_SPEECH_KEY"] = unquote(bare_lines[0])
    return settings


def is_legacy_bare_key_config(path=CONFIG_PATH):
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return False
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    return bool(lines) and not any("=" in ln for ln in lines)


def load_config_file(path=CONFIG_PATH, warn=True):
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return {}
    if warn:
        try:
            mode = os.stat(path).st_mode
        except OSError:
            mode = 0
        # The file holds an API key, so anything readable or writable beyond the
        # owner is a leak worth flagging (600 is what `squawk setup` writes).
        if mode & 0o177:
            print(f"squawk: warning: {path} is mode {mode & 0o777:03o} "
                  f"(holds your API key) — run: chmod 600 {path}", file=sys.stderr)
    return parse_config_text(text)


def load_config(path=CONFIG_PATH):
    # Precedence: built-in defaults < config file < environment. The file is the
    # durable store because squawk usually runs from the hold-to-talk helper (or
    # an iTerm coprocess) with a bare environment that never sources ~/.zshrc;
    # env vars still win so one-off overrides work when testing from a shell.
    values = dict(DEFAULTS)
    values.update({k: v for k, v in load_config_file(path).items() if v != ""})
    for key in SETTING_KEYS:
        env = os.environ.get(key)
        if env is not None:
            values[key] = env

    def as_float(key):
        try:
            return float(values[key])
        except (TypeError, ValueError):
            print(f"squawk: warning: {key}={values[key]!r} is not a number; "
                  f"using {DEFAULTS[key]}", file=sys.stderr)
            return float(DEFAULTS[key])

    indicator = values["SQUAWK_INDICATOR"].strip().lower()
    if indicator not in INDICATOR_MODES:
        print(f"squawk: warning: SQUAWK_INDICATOR={values['SQUAWK_INDICATOR']!r} is not one of "
              f"{', '.join(INDICATOR_MODES)}; using {DEFAULT_INDICATOR}", file=sys.stderr)
        indicator = DEFAULT_INDICATOR

    return Config(
        speech_url=values["SQUAWK_SPEECH_URL"].rstrip("/"),
        speech_key=values["SQUAWK_SPEECH_KEY"].strip(),
        stt_model=values["SQUAWK_STT_MODEL"],
        silence_stop=as_float("SQUAWK_SILENCE_STOP"),
        max_seconds=as_float("SQUAWK_MAX_SECONDS"),
        input_device=values["SQUAWK_INPUT_DEVICE"],
        indicator=indicator,
        indicator_glyphs={s: values[_glyph_key(s)] for s in INDICATOR_STATES},
        indicator_colors={s: values[_color_key(s)].strip() for s in INDICATOR_STATES},
    )


def log(message):
    # Wall-clock timestamp (ms) so the ptt-start/ptt-stop/worker sequence can be
    # reconstructed across the separate processes that make up one dictation.
    ts = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
    print(f"squawk {ts}: {message}", file=sys.stderr)


def play_sound(path):
    try:
        subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ── Visual mode indicator ────────────────────────────────────────────────────
# The indicator is painted by writing terminal escape sequences straight to the
# iTerm session's tty device. This is deliberately NOT the inject_into_iterm()
# path: that uses the scripting `write text` verb, which delivers characters to
# the session as though typed — the shell would swallow them. Escape sequences
# have to reach iTerm's own parser, and writing to the tty is how that happens.
#
# Every operation here is best-effort and silent on failure, exactly like
# play_sound(). The indicator is a convenience layer over a working pipeline and
# must never be able to break dictation.


def _osc(payload):
    """An OSC escape sequence, BEL-terminated (what iTerm expects)."""
    return f"\033]{payload}\007"


def _b64(text):
    return base64.b64encode((text or "").encode("utf-8")).decode("ascii")


def _write_tty(tty, data):
    """Write to a tty device without ever blocking or raising.

    O_NONBLOCK matters: the target tty belongs to another process, and squawk
    writes to it from the keystroke-hot path. A full or wedged terminal must
    cost us nothing, so a short write or EAGAIN is simply dropped.
    """
    # The /dev/ guard matters because one tty value round-trips through the lock
    # file: even if that were ever tampered with, this can only ever write to a
    # device node, never to an arbitrary file.
    if not tty or not data or not tty.startswith("/dev/"):
        return
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return  # vanished, not a tty, or not ours to write
    try:
        os.write(fd, data.encode("utf-8"))
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def current_tty():
    """The tty this process is already attached to, if any.

    Free — no subprocess. Available when squawk runs inside a session (`dictate`
    from a shell or an iTerm coprocess). stderr is checked before stdout because
    `dictate` writes the transcript to stdout, which is routinely a pipe.
    """
    for stream in (sys.stderr, sys.stdout, sys.stdin):
        try:
            return os.ttyname(stream.fileno())
        except (OSError, ValueError, AttributeError):
            continue
    return None


def resolve_session_tty():
    """The tty of the iTerm session to paint into, or None.

    Prefers this process's own tty, which costs nothing. Under push-to-talk the
    helper spawns squawk with no terminal at all, so it falls back to asking
    iTerm for the frontmost session — correct, because the trigger only fires
    while iTerm is frontmost. That ~200 ms round-trip is why the result is cached
    in the lock file and resolved at most once per dictation.
    """
    tty = current_tty()
    if tty and os.access(tty, os.W_OK):
        return tty
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "iTerm2" to get tty of current session of current window'],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tty = result.stdout.strip()
    if tty.startswith("/dev/") and os.access(tty, os.W_OK):
        return tty
    return None


def indicator_sequences(cfg, state):
    """The escape sequences that paint `state`, honouring the configured surfaces.
    A state of None means "clear". Returns "" when nothing should be written."""
    if cfg.indicator == "off":
        return ""
    parts = []
    glyph = cfg.indicator_glyphs.get(state, "") if state else ""
    if cfg.indicator in ("both", "badge"):
        # The badge is session chrome, not grid content, so a full-screen TUI and
        # a shell prompt both leave it alone. The user var is invisible unless the
        # user binds it in an iTerm status-bar component — one extra free write
        # that makes that opt-in surface work.
        parts.append(_osc("1337;SetBadgeFormat=" + _b64(glyph)))
        parts.append(_osc("1337;SetUserVar=squawk=" + _b64(glyph)))
    if cfg.indicator in ("both", "cursor"):
        color = cfg.indicator_colors.get(state, "") if state else ""
        # OSC 112 restores the profile's own cursor colour, which is more robust
        # than trying to remember and put back whatever was there before.
        parts.append(_osc(f"12;{color}") if color else _osc("112"))
    return "".join(parts)


def set_indicator(cfg, state, tty):
    """Paint `state` into the given session. Silent no-op without a usable tty."""
    _write_tty(tty, indicator_sequences(cfg, state))


def clear_indicator(cfg, tty, force=False):
    """Remove indicator state from the given session.

    Teardown clears *every* surface rather than only the configured ones, so a
    changed config or residue from a crashed run can never strand a glyph or a
    recoloured cursor. `force` clears even when the indicator is disabled, which
    is what `squawk reset-indicator` needs.
    """
    if cfg.indicator == "off" and not force:
        return
    _write_tty(tty, _osc("1337;SetBadgeFormat=") + _osc("1337;SetUserVar=squawk=")
               + _osc("112"))


def flash_indicator(cfg, state, tty):
    """Show a terminal state briefly, then clear. Only ever called from the
    detached worker, which has already delivered the transcript — so this wait is
    not on any path the user is waiting on."""
    if not tty or cfg.indicator == "off":
        return
    set_indicator(cfg, state, tty)
    time.sleep(INDICATOR_FLASH_SECONDS)
    clear_indicator(cfg, tty)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock():
    try:
        with open(LOCK_PATH) as f:
            data = json.load(f)
        # `tty` is read with .get() like the rest: a lock written by an older
        # squawk simply yields None and the visual indicator is skipped.
        return data.get("pid"), data.get("wav"), data.get("session"), data.get("tty")
    except (OSError, ValueError):
        return None, None, None, None


def acquire_lock(pid):
    existing_pid, _, _, _ = read_lock()
    if existing_pid and pid_alive(existing_pid):
        return False
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        json.dump({"pid": pid, "wav": None}, f)
    return True


def write_lock_wav(pid, wav_path, session=None, tty=None):
    with open(LOCK_PATH, "w") as f:
        json.dump({"pid": pid, "wav": wav_path, "session": session, "tty": tty}, f)


def release_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def sox_env(cfg):
    env = os.environ.copy()
    if cfg.input_device and cfg.input_device != "default":
        env["AUDIODEV"] = cfg.input_device
    return env


def kill_stale_recorders():
    """Kill any lingering squawk recorder before starting a fresh one.

    A sox recorder that outlives its ptt-stop keeps holding the default mic; the
    NEXT recording then contends for the device, over-records, and won't respond
    to its stop signal for many seconds (observed: a few seconds of speech
    captured as 26s of audio, 23s to stop). Guaranteeing exactly one recorder
    owns the mic prevents that pile-up. The pattern matches only the sox
    recorder (its args contain "squawk-ptt-…​.wav"), never the transcription
    worker (whose args also mention the file but not "sox ")."""
    try:
        subprocess.run(
            ["/usr/bin/pkill", "-9", "-f", r"sox .*squawk-ptt-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def wrap_raw_as_wav(raw_path):
    """Wrap headerless PCM (what the ptt recorder writes) into a real WAV file
    for upload. Because raw capture has no header to finalize, the recorder can
    be SIGKILLed for an instant stop and every byte written is still valid audio
    — no waiting for a graceful WAV flush. Returns the wav path, or None if there
    is no audio."""
    try:
        with open(raw_path, "rb") as f:
            pcm = f.read()
    except OSError:
        return None
    if len(pcm) < 320:  # < ~10ms of 16kHz/16-bit mono — effectively empty
        return None
    wav_path = raw_path + ".wav"
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)      # 16-bit
        w.setframerate(16000)
        w.writeframes(pcm)
    return wav_path


def capture_audio(wav_path, cfg):
    cmd = [
        SOX, "-d", "-c", "1", "-r", "16000", "-b", "16", wav_path,
        "silence", "1", "0.1", "2%", "1", str(cfg.silence_stop), "2%",
    ]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env=sox_env(cfg),
    )
    start = time.monotonic()
    while True:
        try:
            proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            pass
        elapsed = time.monotonic() - start
        size = os.path.getsize(wav_path) if os.path.exists(wav_path) else 0
        if elapsed >= cfg.max_seconds or size >= MAX_UPLOAD_BYTES:
            # SIGINT (not SIGTERM) so sox finalizes the WAV header like a manual stop.
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            break

    if not os.path.exists(wav_path) or os.path.getsize(wav_path) <= WAV_HEADER_BYTES:
        return None
    return wav_path


def build_multipart(wav_path, model, boundary):
    with open(wav_path, "rb") as f:
        wav_bytes = f.read()
    parts = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="model"\r\n\r\n',
        model.encode() + b"\r\n",
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{os.path.basename(wav_path)}"\r\n'.encode(),
        b"Content-Type: audio/wav\r\n\r\n",
        wav_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts)


class SpeechHTTPError(RuntimeError):
    """A non-2xx response from the speech endpoint, carrying enough detail for
    `check` to turn it into backend-appropriate guidance."""

    def __init__(self, status, body=""):
        super().__init__(f"STT endpoint returned HTTP {status}")
        self.status = status
        self.body = body


def auth_headers(cfg):
    # Keyless local servers (whisper.cpp `server`, LM Studio) reject or simply
    # ignore an empty Bearer; sending no Authorization header at all is what they
    # expect, and hosted providers are never configured without a key.
    return {"Authorization": f"Bearer {cfg.speech_key}"} if cfg.speech_key else {}


def open_speech_connection(cfg):
    parsed = urllib.parse.urlsplit(cfg.speech_url)
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parsed.hostname, parsed.port, timeout=CONNECT_TIMEOUT)
    return conn, parsed.path.rstrip("/")


def transcribe(wav_path, cfg):
    conn, base = open_speech_connection(cfg)
    path = base + "/audio/transcriptions"
    boundary = uuid.uuid4().hex
    body = build_multipart(wav_path, cfg.stt_model, boundary)

    try:
        conn.connect()
        conn.sock.settimeout(TOTAL_TIMEOUT)
        headers = dict(auth_headers(cfg))
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
    finally:
        conn.close()

    if resp.status < 200 or resp.status >= 300:
        raise SpeechHTTPError(resp.status, data.decode("utf-8", "replace")[:400])
    payload = json.loads(data.decode("utf-8"))
    return payload.get("text", "")


def sanitize(text, keep_newlines):
    text = (text or "").strip()
    if not keep_newlines:
        text = re.sub(r"\s*\n+\s*", " ", text).strip()
    return text


INJECT_APPLESCRIPT = """on run argv
  set targetId to item 1 of argv
  set t to item 2 of argv
  tell application "iTerm2"
    if targetId is "" then
      -- No originating session was recorded (the coprocess environment may
      -- lack ITERM_SESSION_ID). Push-to-talk always means "the session I am
      -- typing in", so target the frontmost session.
      tell current session of current window to write text t newline no
      return "ok"
    end if
    repeat with w in windows
      repeat with tb in tabs of w
        repeat with s in sessions of tb
          if (id of s) is targetId then
            tell s to write text t newline no
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "notfound"
end run
"""


def inject_into_iterm(session_id, text):
    """Send text to an iTerm session as though typed (no trailing newline, so it
    is NOT executed). Targets the given session id, or the frontmost session if
    none was captured. Returns True only if the text was delivered."""
    try:
        result = subprocess.run(
            ["osascript", "-", session_id or "", text],
            input=INJECT_APPLESCRIPT, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"osascript injection failed: {exc}")
        return False
    if result.stdout.strip() == "ok":
        return True
    log(f"injection not delivered (target={session_id!r}): "
        f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}")
    return False


def copy_to_clipboard(text):
    try:
        subprocess.run(["pbcopy"], input=text, text=True, timeout=5, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def spawn_finish(pid, wav_path, session, tty, keep_audio, keep_newlines):
    """Launch the transcription+delivery worker fully detached from this
    short-lived keystroke hook, which must return immediately."""
    args = [sys.executable, os.path.realpath(__file__), "_finish", wav_path or "",
            "--session", session or "", "--pid", str(pid or 0), "--tty", tty or ""]
    if keep_audio:
        args.append("--keep-audio")
    if keep_newlines:
        args.append("--keep-newlines")
    try:
        # The worker is detached with no terminal, so its stderr goes to a log
        # file — the only way to debug delivery failures after the fact.
        with open(WORKER_LOG, "ab") as logf:
            subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=logf, start_new_session=True,
            )
    except OSError as exc:
        log(f"failed to start transcription worker: {exc}")
        play_sound(SOUND_ERROR)
        return False
    return True


def transcribe_and_emit(wav_path, cfg, keep_newlines, tty=None):
    if not wav_path or not os.path.exists(wav_path) or os.path.getsize(wav_path) <= WAV_HEADER_BYTES:
        log("microphone capture failed (no audio captured)")
        play_sound(SOUND_ERROR)
        flash_indicator(cfg, "error", tty)
        return 1

    set_indicator(cfg, "transcribing", tty)
    try:
        text = transcribe(wav_path, cfg)
    except Exception as exc:
        log(f"transcription failed: {describe_speech_failure(cfg, exc)}")
        play_sound(SOUND_ERROR)
        flash_indicator(cfg, "error", tty)
        return 1

    text = sanitize(text, keep_newlines)
    if not text:
        play_sound(SOUND_EMPTY)
        flash_indicator(cfg, "empty", tty)
        return 0

    sys.stdout.write(text)
    sys.stdout.flush()
    play_sound(SOUND_SUCCESS)
    flash_indicator(cfg, "success", tty)
    return 0


def cmd_dictate(cfg, keep_audio=False, keep_newlines=False):
    if not acquire_lock(os.getpid()):
        log("another squawk dictate is already running")
        play_sound(SOUND_ERROR)
        return 1

    wav_path = None
    # `dictate` runs inside a session, so its own tty is the target — no
    # osascript round-trip needed here, unlike the push-to-talk path.
    tty = current_tty()
    try:
        play_sound(SOUND_START)
        clear_indicator(cfg, tty)
        set_indicator(cfg, "recording", tty)
        fd, wav_path = tempfile.mkstemp(suffix=".wav", dir=tempfile.gettempdir(), prefix="squawk-")
        os.close(fd)
        write_lock_wav(os.getpid(), wav_path, tty=tty)

        captured = capture_audio(wav_path, cfg)
        if not captured:
            log("microphone capture failed (no audio captured)")
            play_sound(SOUND_ERROR)
            flash_indicator(cfg, "error", tty)
            return 1

        return transcribe_and_emit(wav_path, cfg, keep_newlines, tty)
    finally:
        clear_indicator(cfg, tty)
        if wav_path and os.path.exists(wav_path):
            if keep_audio:
                log(f"kept audio at {wav_path}")
            else:
                try:
                    os.remove(wav_path)
                except OSError:
                    pass
        release_lock()


def cmd_ptt_start(cfg):
    # Free the mic from any orphaned recorder first (acquire_lock already
    # reclaims a stale lock left by a crashed run), so a fresh recording always
    # starts from a clean slate with exactly one sox owning the device.
    kill_stale_recorders()
    if not acquire_lock(os.getpid()):
        log("a recording is already in progress")
        return 0

    # Capture headerless raw PCM (not a WAV): with no header to finalize, the
    # recorder can be SIGKILLed for a truly instant stop on release and the bytes
    # are still complete, valid audio. The worker wraps them into a WAV.
    fd, raw_path = tempfile.mkstemp(suffix=".raw", dir=tempfile.gettempdir(), prefix="squawk-ptt-")
    os.close(fd)

    cmd = [
        SOX, "-d", "-c", "1", "-r", "16000", "-b", "16",
        "-e", "signed-integer", "-t", "raw", raw_path,
        "trim", "0", str(cfg.max_seconds),
    ]
    try:
        # No start_new_session here: sox must stay in SquawkPTT's TCC
        # responsibility chain so the mic grant applies, and it survives this
        # process exiting anyway (it gets reparented, nobody signals its group).
        # sox stderr goes to a log file — device-open failures are otherwise
        # indistinguishable from silence.
        with open(SOX_LOG, "ab") as soxlog:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=soxlog, env=sox_env(cfg),
            )
    except OSError as exc:
        log(f"could not start recorder ({SOX}): {exc}")
        try:
            os.remove(raw_path)
        except OSError:
            pass
        release_lock()
        play_sound(SOUND_ERROR)
        return 1
    # $ITERM_SESSION_ID looks like "w0t1p0:<UUID>"; the session's scripting id
    # is just the UUID. It is only present when squawk runs inside an iTerm
    # session (coprocess/manual). Under the hold-to-talk trigger it is absent,
    # leaving session empty — the worker then targets iTerm's frontmost
    # session, which is correct because the trigger only fires while iTerm is
    # the frontmost application.
    session = os.environ.get("ITERM_SESSION_ID", "").split(":", 1)[-1]
    log(f"ptt-start: recording (sox pid={proc.pid}) -> {raw_path}")
    play_sound(SOUND_START)

    # Only now resolve the session tty. Under push-to-talk this costs an
    # osascript round-trip (~200 ms), so it deliberately happens AFTER sox is
    # already capturing and after the start cue has fired — the audible ack stays
    # instant and recording latency is untouched. Cached in the lock file so
    # ptt-stop and the worker paint for free.
    tty = resolve_session_tty()
    write_lock_wav(proc.pid, raw_path, session, tty)
    # Clear before painting: residue from a run that was killed before it could
    # clean up must not survive into this one.
    clear_indicator(cfg, tty)
    set_indicator(cfg, "recording", tty)
    return 0


def cmd_ptt_stop(cfg, keep_audio=False, keep_newlines=False):
    # Called by SquawkPTT on Space release. The helper only fires this when a
    # hold actually started recording, but stay defensive — do the bare minimum
    # (acknowledge the release, kill the recorder, hand the rest to a detached
    # worker), then return at once. If nothing was recording, silently no-op.
    pid, raw_path, session, tty = read_lock()
    if not pid or not pid_alive(pid):
        # Nothing was recording (e.g. a plain space tap) — silent no-op, and
        # nothing was painted either, so there is nothing to clear.
        release_lock()
        return 0

    # Releasing Space is deliberate: you stopped talking, so stop NOW. SIGKILL
    # (not SIGINT) can't be caught, blocked, or delayed by whatever state sox is
    # in — it dies instantly. That is safe only because we capture raw PCM (no
    # WAV header to finalize), so the bytes already on disk are complete audio.
    log(f"ptt-stop: release -> SIGKILL sox pid={pid}")
    play_sound(SOUND_ACK)  # immediate "released, on it" cue
    # The tty was resolved at ptt-start, so this is a single free write — the
    # "thinking" glyph appears the moment Space comes up, which is the whole
    # point of the feature: the 1-3 s transcription gap becomes visible.
    set_indicator(cfg, "transcribing", tty)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    release_lock()

    if not spawn_finish(pid, raw_path, session, tty, keep_audio, keep_newlines):
        # The worker never started, so nothing downstream will ever clear the
        # "thinking" glyph. Show the failure and clean up here instead of
        # stranding it on screen. Safe to block briefly: the helper spawned this
        # process fire-and-forget and is not waiting on it.
        flash_indicator(cfg, "error", tty)
        return 1
    return 0


def cmd_finish(cfg, raw_path, session, pid=0, tty=None, keep_audio=False, keep_newlines=False):
    """Detached worker: confirm the recorder is dead, wrap the raw PCM into a
    WAV, transcribe it, and deliver the text into the originating iTerm session
    (falling back to the clipboard). Runs with no coprocess attached, so nothing
    here — the wait, the network round-trip — can freeze the terminal.

    This is also the only process that knows how a dictation *ended*, so it owns
    every terminal indicator state and, critically, the teardown: the `finally`
    below guarantees no glyph or recoloured cursor outlives the worker, whichever
    path it exits by."""
    t_start = time.monotonic()
    log(f"worker start: raw={raw_path} session={session!r} recorder_pid={pid}")

    # A plain SIGTERM/SIGINT would tear this process down without unwinding, so
    # the `finally` below would never run and the "thinking" glyph would be left
    # stranded in the session. Turning the signal into a normal exception path
    # means an interrupted worker still cleans up after itself. SIGKILL remains
    # uncatchable by definition — `squawk reset-indicator` is the remedy there.
    def _on_signal(signum, *_):
        raise KeyboardInterrupt(f"signal {signum}")

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (OSError, ValueError):
            pass

    wav_path = None
    try:
        # ptt-stop already SIGKILLed sox, so it is dying essentially now. A brief
        # poll (cap ~0.5s) confirms the kernel released the file; because raw PCM
        # has no header to finalize, there is nothing to "flush" and no reason to
        # wait beyond the process actually exiting.
        if pid:
            for _ in range(25):  # up to ~0.5s
                if not pid_alive(pid):
                    break
                time.sleep(0.02)
        t_flushed = time.monotonic()

        wav_path = wrap_raw_as_wav(raw_path)
        if not wav_path:
            log("microphone capture failed (no audio captured)")
            play_sound(SOUND_ERROR)
            flash_indicator(cfg, "error", tty)
            return 1

        audio_sec = os.path.getsize(raw_path) / (16000 * 2)
        try:
            text = transcribe(wav_path, cfg)
            t_transcribed = time.monotonic()
            log(f"TIMING: wait-for-sox={t_flushed - t_start:.2f}s "
                f"transcribe={t_transcribed - t_flushed:.2f}s "
                f"audio={audio_sec:.1f}s total-in-worker={t_transcribed - t_start:.2f}s")
        except Exception as exc:
            log(f"transcription failed: {describe_speech_failure(cfg, exc)}")
            play_sound(SOUND_ERROR)
            flash_indicator(cfg, "error", tty)
            return 1

        text = sanitize(text, keep_newlines)
        if not text:
            play_sound(SOUND_EMPTY)
            flash_indicator(cfg, "empty", tty)
            return 0

        if inject_into_iterm(session, text):
            log(f"delivered {len(text)} chars")
            play_sound(SOUND_SUCCESS)
            # Flash only after the text has landed, so the glyph never implies
            # success before delivery actually happened.
            flash_indicator(cfg, "success", tty)
            return 0

        # Originating session is gone (closed tab) or we have no session id:
        # stash the transcript on the clipboard rather than typing it somewhere
        # unexpected.
        if copy_to_clipboard(text):
            log("originating session unavailable; transcript copied to clipboard")
            play_sound(SOUND_SUCCESS)
            flash_indicator(cfg, "success", tty)
            return 0

        # Log only the length, never the text — the transcript is the user's
        # private dictation and must not be written to disk on a delivery failure.
        log(f"could not deliver transcript ({len(text)} chars)")
        play_sound(SOUND_ERROR)
        flash_indicator(cfg, "error", tty)
        return 1
    except KeyboardInterrupt as exc:
        # Raised by the signal handler above. Logged rather than traced so the
        # worker log stays readable; the `finally` does the actual cleanup.
        log(f"worker interrupted ({exc}) — clearing indicator")
        return 1
    finally:
        # Unconditional teardown. Every return above has already flashed its own
        # terminal state, but an unexpected exception must not leave the
        # "thinking" glyph and a recoloured cursor stranded in the session.
        clear_indicator(cfg, tty)
        for p in (raw_path, wav_path):
            if p and os.path.exists(p):
                if keep_audio:
                    log(f"kept audio at {p}")
                else:
                    try:
                        os.remove(p)
                    except OSError:
                        pass


def cmd_reset_indicator(cfg):
    """Clear a stuck mode indicator.

    The pipeline tears itself down on every path, but a process killed outright
    (or a config changed mid-dictation) can still strand a glyph or a recoloured
    cursor — which is worse than having no indicator at all. Clears the session
    recorded in the lock file as well as the invoking one, and forces the write
    even when the indicator is configured off, since that is exactly the state a
    user who just disabled it would want cleaned up.
    """
    _, _, _, lock_tty = read_lock()
    targets = []
    for tty in (lock_tty, current_tty(), resolve_session_tty()):
        if tty and tty not in targets:
            targets.append(tty)
    if not targets:
        print("squawk: no iTerm session found to clear", file=sys.stderr)
        return 1
    for tty in targets:
        clear_indicator(cfg, tty, force=True)
    print("squawk: cleared mode indicator in " + ", ".join(targets))
    return 0


def probe_mic(cfg):
    fd, path = tempfile.mkstemp(suffix=".wav", dir=tempfile.gettempdir(), prefix="squawk-check-")
    os.close(fd)
    try:
        cmd = [SOX, "-d", "-c", "1", "-r", "16000", "-b", "16", path, "trim", "0", "0.5"]
        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=sox_env(cfg), timeout=10,
        )
        if result.returncode != 0 or not os.path.exists(path):
            return False, "mic capture failed to run (sox error)"
        with wave.open(path, "rb") as w:
            frames = w.readframes(w.getnframes())
        if not frames or frames.count(0) == len(frames):
            return False, "captured audio is silent (no signal from microphone)"
        return True, "mic OK"
    except subprocess.TimeoutExpired:
        return False, "mic capture timed out"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def key_guidance(cfg):
    if not cfg.speech_key:
        return (f"no API key configured, and {cfg.speech_url} requires one — "
                f"run `squawk setup`")
    return "API key rejected — run `squawk setup` to re-enter it (or set SQUAWK_SPEECH_KEY)"


def model_guidance(cfg):
    return (f"endpoint reachable but does not serve model {cfg.stt_model!r} — "
            f"pick another with `squawk setup`")


def describe_connection_error(cfg, exc):
    if isinstance(exc, ConnectionRefusedError) or isinstance(
            getattr(exc, "__cause__", None), ConnectionRefusedError):
        return (f"no server reachable at {cfg.speech_url} — "
                f"is your local STT server running?")
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return f"timed out connecting to {cfg.speech_url}"
    if isinstance(exc, socket.gaierror):
        return f"cannot resolve host in {cfg.speech_url} ({exc})"
    return f"connection failed: {exc}"


def describe_http_error(cfg, exc):
    if exc.status in (401, 403):
        return key_guidance(cfg)
    if exc.status in (400, 404, 422) and cfg.stt_model.lower() in exc.body.lower():
        return model_guidance(cfg)
    if exc.status == 404:
        return (f"{cfg.speech_url} does not implement the OpenAI "
                f"/audio/transcriptions API (HTTP 404)")
    detail = f" — {exc.body.strip()}" if exc.body.strip() else ""
    return f"endpoint returned HTTP {exc.status}{detail}"


def describe_speech_failure(cfg, exc):
    """Human-readable cause for a failed transcription, whichever way it failed."""
    if isinstance(exc, SpeechHTTPError):
        return describe_http_error(cfg, exc)
    if isinstance(exc, OSError):
        return describe_connection_error(cfg, exc)
    return str(exc)


def check_endpoint_health(cfg):
    """Probe `GET /models`. Returns (status, message) with status one of
    "ok" / "warn" / "fail". Only a rejected key or an unreachable server is a
    hard failure: many local servers omit or differently shape /models, so a
    surprising answer there is a warning and the transcription round-trip is the
    authoritative test."""
    conn, base = open_speech_connection(cfg)
    try:
        conn.connect()
        conn.sock.settimeout(TOTAL_TIMEOUT)
        conn.request("GET", base + "/models", headers=auth_headers(cfg))
        resp = conn.getresponse()
        data = resp.read()
    except Exception as exc:
        return "fail", describe_connection_error(cfg, exc)
    finally:
        conn.close()

    if resp.status in (401, 403):
        return "fail", key_guidance(cfg)
    if resp.status == 404:
        return "warn", "endpoint does not expose /models — relying on the round-trip below"
    if resp.status < 200 or resp.status >= 300:
        return "warn", f"/models returned HTTP {resp.status} — relying on the round-trip below"
    try:
        payload = json.loads(data.decode("utf-8"))
    except ValueError:
        return "warn", "/models returned invalid JSON — relying on the round-trip below"
    ids = [m.get("id") for m in payload.get("data", []) if m.get("id")]
    if not ids:
        return "warn", "/models listed no models — relying on the round-trip below"
    if cfg.stt_model not in ids:
        return "warn", (f"model {cfg.stt_model} not listed by /models "
                        f"(listed: {', '.join(ids[:8])}{'…' if len(ids) > 8 else ''}) — "
                        f"relying on the round-trip below")
    return "ok", f"endpoint OK (serves {cfg.stt_model})"


def build_canned_audio():
    fd, path = tempfile.mkstemp(suffix=".wav", dir=tempfile.gettempdir(), prefix="squawk-canned-")
    os.close(fd)
    subprocess.run(
        [SOX, "-n", "-c", "1", "-r", "16000", "-b", "16", path, "synth", "1", "sine", "440"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10,
    )
    return path


def print_backend(cfg):
    print(f"[backend]   {cfg.speech_url or '(none configured)'}  model={cfg.stt_model or '(none)'}"
          f"  key={'set' if cfg.speech_key else 'none'}")


def verify_backend(cfg):
    """Probe the configured speech endpoint and transcribe a synthesized tone.
    Prints one line per stage and returns True if the backend is usable. Shared
    by `squawk check` and the verification pass at the end of `squawk setup`."""
    if not cfg.speech_url:
        print("[endpoint]  FAIL - no backend configured — run `squawk setup`")
        print("[roundtrip] SKIPPED - no backend configured")
        return False

    host = urllib.parse.urlsplit(cfg.speech_url).hostname or ""
    if not cfg.speech_key and host in KEY_REQUIRED_HOSTS:
        print(f"[endpoint]  FAIL - {key_guidance(cfg)}")
        print("[roundtrip] SKIPPED - no API key configured")
        return False

    ep_status, ep_msg = check_endpoint_health(cfg)
    print(f"[endpoint]  {ep_status.upper()} - {ep_msg}")
    if ep_status == "fail":
        print("[roundtrip] SKIPPED - endpoint unreachable or key rejected")
        return False

    # A warning on /models is not fatal: the round-trip decides. Its verdict also
    # retro-justifies the warning — if transcription works, the model is served.
    canned_path = None
    try:
        canned_path = build_canned_audio()
        start = time.monotonic()
        text = transcribe(canned_path, cfg)
        latency = time.monotonic() - start
        print(f"[roundtrip] OK - {latency:.2f}s (text: {text!r})")
        return True
    except SpeechHTTPError as exc:
        print(f"[roundtrip] FAIL - {describe_http_error(cfg, exc)}")
        return False
    except Exception as exc:
        print(f"[roundtrip] FAIL - {describe_connection_error(cfg, exc)}")
        return False
    finally:
        if canned_path:
            try:
                os.remove(canned_path)
            except OSError:
                pass


def cmd_check(cfg):
    print_backend(cfg)

    mic_ok, mic_msg = probe_mic(cfg)
    print(f"[mic]       {'OK' if mic_ok else 'FAIL'} - {mic_msg}")
    if not mic_ok:
        log("remediation: System Settings -> Privacy & Security -> Microphone -> enable iTerm")

    backend_ok = verify_backend(cfg)
    return 0 if (mic_ok and backend_ok) else 1


KARABINER_JSON = os.path.expanduser("~/.config/karabiner/karabiner.json")
LEGACY_AGENT_LABEL = "org.elabz.voxptt"
# A legacy Space rule's shell_command touches one of these state paths (squawk
# or the older vox layout), so the fragment is a reliable marker in a rule's
# JSON. `hold_space_dictate` catches the rule by its imported file identifier.
LEGACY_RULE_MARKERS = ("state/squawk/ptt", "state/vox/ptt", "hold_space_dictate")


def _rule_is_legacy(rule):
    """True if a Karabiner complex-modification rule is the legacy squawk/vox
    hold-Space trigger, detected by the trigger-file path it touches."""
    blob = json.dumps(rule)
    return any(marker in blob for marker in LEGACY_RULE_MARKERS)


def cmd_migrate():
    """Cross over from the legacy Karabiner-based trigger to the native tap:
    back up karabiner.json, strip the hold-Space rule from every profile, unload
    the legacy LaunchAgent, and print the exact steps to roll all of it back."""
    did_something = False

    # 1. Karabiner rule: back up, then remove the legacy rule from each profile.
    backup_path = None
    if os.path.exists(KARABINER_JSON):
        try:
            with open(KARABINER_JSON, "r") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"squawk: could not read {KARABINER_JSON}: {exc}", file=sys.stderr)
            return 1

        removed = 0
        for profile in data.get("profiles", []):
            rules = profile.get("complex_modifications", {}).get("rules", [])
            kept = [r for r in rules if not _rule_is_legacy(r)]
            removed += len(rules) - len(kept)
            if kept != rules:
                profile["complex_modifications"]["rules"] = kept

        if removed:
            backup_path = f"{KARABINER_JSON}.squawk-pre-migrate.{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(KARABINER_JSON, backup_path)
            with open(KARABINER_JSON, "w") as f:
                json.dump(data, f, indent=4)
                f.write("\n")
            print(f"Removed {removed} legacy hold-Space rule(s) from {KARABINER_JSON}.")
            print(f"  backup: {backup_path}")
            did_something = True
        else:
            print(f"No legacy hold-Space rule found in {KARABINER_JSON} — nothing to remove.")
    else:
        print(f"No {KARABINER_JSON} — Karabiner was never configured, nothing to remove.")

    # 2. Legacy LaunchAgent: unload it so it stops polling the trigger dir / mic.
    label = LEGACY_AGENT_LABEL
    domain = f"gui/{os.getuid()}"
    proc = subprocess.run(
        ["launchctl", "bootout", f"{domain}/{label}"],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        print(f"Unloaded legacy agent {label}.")
        did_something = True
    else:
        # bootout returns non-zero when the agent isn't loaded — expected on a
        # machine that only ever ran the current sh.squawk.ptt helper.
        print(f"Legacy agent {label} was not loaded — nothing to unload.")

    if not did_something:
        print("\nAlready migrated: no legacy Karabiner rule or agent was present.")
        return 0

    # 3. Rollback instructions — how to restore the old system if needed.
    print("\nMigration complete. The native SquawkPTT event tap now owns Space in")
    print("iTerm; restart the helper (or re-run helper/install.sh) if it was running.")
    print("\nTo roll back to the old Karabiner-based trigger:")
    if backup_path:
        print(f"  1. Restore the rule file: cp '{backup_path}' '{KARABINER_JSON}'")
        print("  2. Restart Karabiner-Elements so it reloads the profile.")
    else:
        print("  1. Re-add the hold-Space rule to Karabiner-Elements.")
    print(f"  3. Reload the legacy agent, e.g.:")
    print(f"       launchctl bootstrap {domain} "
          f"~/Library/LaunchAgents/{label}.plist")
    print("  (or reinstate the previous helper build/rule file from git history.)")
    return 0


# ---------------------------------------------------------------------------
# Packaging: agent lifecycle (`install-agent`/`uninstall`) and `doctor`.
# ---------------------------------------------------------------------------

APP_NAME = "SquawkPTT.app"
APP_DST = os.path.expanduser("~/Applications/SquawkPTT.app")
AGENT_LABEL = "sh.squawk.ptt"
AGENT_PLIST_NAME = "sh.squawk.ptt.plist"
AGENT_PLIST_DST = os.path.expanduser("~/Library/LaunchAgents/sh.squawk.ptt.plist")
HELPER_LOG = os.path.expanduser("~/Library/Logs/squawkptt.log")

# Exact System Settings panes for each permission, so `doctor`/`uninstall` can
# open the right place instead of "somewhere under Privacy & Security".
PANE_MIC = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
PANE_AX = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
PANE_INPUT = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
PANE_AUTOMATION = "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"


def helper_resource_dirs():
    """Candidate dirs that hold the helper's build inputs (build.sh, Info.plist,
    squawkptt.m, the plist template) or a prebuilt SquawkPTT.app, most-specific
    first: an explicit override, the Homebrew keg's libexec, the per-user share
    dir the curl installer populates, and the repo layout beside this script."""
    real = os.path.realpath(__file__)
    bindir = os.path.dirname(real)
    prefix = os.path.dirname(bindir)  # under Homebrew, bin/.. is the keg prefix
    candidates = [
        os.environ.get("SQUAWK_HELPER_DIR"),
        os.path.join(prefix, "libexec", "squawk"),
        os.path.expanduser("~/.local/share/squawk/helper"),
        os.path.join(bindir, "helper"),        # repo: squawk.py beside helper/
        os.path.join(bindir, "..", "helper"),  # defensive
    ]
    seen, out = set(), []
    for d in candidates:
        if not d:
            continue
        ad = os.path.abspath(d)
        if ad not in seen:
            seen.add(ad)
            out.append(ad)
    return out


def find_helper_dir():
    """First candidate dir carrying a prebuilt app or the build inputs."""
    for d in helper_resource_dirs():
        if os.path.isdir(os.path.join(d, APP_NAME)) or os.path.exists(os.path.join(d, "build.sh")):
            return d
    return None


def agent_running():
    """True if the LaunchAgent is loaded and its process is up."""
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    return "state = running" in r.stdout or "pid = " in r.stdout


def helper_cli_path():
    """The squawk CLI the helper will spawn, and where that answer came from.

    Mirrors resolve_squawk_path() in squawkptt.m: the SQUAWK_CLI recorded in the
    installed LaunchAgent wins, then ~/bin/squawk, then the Homebrew prefixes.
    Returns (path, source) or (None, None). The path is returned even when it is
    missing or non-executable so the caller can say which path is broken."""
    try:
        with open(AGENT_PLIST_DST) as f:
            plist = f.read()
    except OSError:
        plist = ""
    # Minimal, dependency-free read of <key>SQUAWK_CLI</key><string>…</string>.
    marker = "<key>SQUAWK_CLI</key>"
    idx = plist.find(marker)
    if idx != -1:
        start = plist.find("<string>", idx)
        end = plist.find("</string>", start) if start != -1 else -1
        if start != -1 and end != -1:
            recorded = plist[start + len("<string>"):end].strip()
            if recorded and recorded != "__SQUAWK_CLI__":
                return recorded, "recorded in the LaunchAgent"

    for candidate in (os.path.expanduser("~/bin/squawk"),
                      "/opt/homebrew/bin/squawk", "/usr/local/bin/squawk"):
        if os.access(candidate, os.X_OK):
            return candidate, "fallback — re-run install-agent to record it"
    return None, None


def cmd_install_agent():
    """Place SquawkPTT.app in ~/Applications, install and load its LaunchAgent,
    and confirm it is running. The Homebrew formula delegates this stateful,
    permission-bound step to the CLI rather than doing it itself."""
    helper_dir = find_helper_dir()
    if not helper_dir:
        print("squawk: could not locate the SquawkPTT helper resources "
              "(a prebuilt SquawkPTT.app or build.sh). Set SQUAWK_HELPER_DIR to "
              "the helper directory, or reinstall squawk.", file=sys.stderr)
        return 1

    os.makedirs(os.path.expanduser("~/Applications"), exist_ok=True)
    # Stop a running instance before replacing the binary — launchctl holds it open.
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                   capture_output=True, text=True)

    prebuilt = os.path.join(helper_dir, APP_NAME)
    if os.path.isdir(prebuilt):
        if os.path.isdir(APP_DST):
            shutil.rmtree(APP_DST, ignore_errors=True)
        shutil.copytree(prebuilt, APP_DST)
        print(f"Installed prebuilt {APP_NAME} -> {APP_DST}")
    else:
        build = os.path.join(helper_dir, "build.sh")
        print(f"Building {APP_NAME} from source in {helper_dir} …")
        if subprocess.run(["/bin/sh", build, APP_DST]).returncode != 0:
            print("squawk: helper build failed — is Xcode Command Line Tools "
                  "installed? Run: xcode-select --install", file=sys.stderr)
            return 1

    plist_src = os.path.join(helper_dir, AGENT_PLIST_NAME)
    if not os.path.exists(plist_src):
        print(f"squawk: LaunchAgent template not found at {plist_src}", file=sys.stderr)
        return 1
    # The plist ships with __HOME__ placeholders (launchd can't expand ~/$HOME);
    # substitute the real home so Program and the log path are absolute.
    # __SQUAWK_CLI__ becomes the absolute path of the CLI running right now, which
    # the helper spawns for ptt-start/ptt-stop. It must be recorded rather than
    # assumed: a Homebrew install leaves the CLI in the keg (linked into
    # /opt/homebrew/bin) and never creates ~/bin/squawk, so the helper's old
    # hardcoded ~/bin/squawk path did not exist at all on a brew-only machine.
    # abspath, deliberately NOT realpath: under Homebrew the CLI is reached via
    # the stable symlink /opt/homebrew/bin/squawk, and resolving it would pin the
    # plist to a versioned keg path that the next `brew upgrade` deletes.
    cli_path = os.path.abspath(__file__)
    with open(plist_src) as f:
        plist = (f.read()
                 .replace("__HOME__", os.path.expanduser("~"))
                 .replace("__SQUAWK_CLI__", cli_path))
    os.makedirs(os.path.dirname(AGENT_PLIST_DST), exist_ok=True)
    with open(AGENT_PLIST_DST, "w") as f:
        f.write(plist)

    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", AGENT_PLIST_DST],
                       capture_output=True, text=True)
    if r.returncode != 0:
        detail = r.stderr.strip() or r.stdout.strip()
        print(f"squawk: launchctl bootstrap reported: {detail}", file=sys.stderr)

    if agent_running():
        print(f"SquawkPTT agent loaded and running (label {AGENT_LABEL}).")
        print(f"Push-to-talk will run: {cli_path}")
        print("Next: grant permissions when prompted, then run `squawk doctor`.")
        return 0
    print("squawk: agent installed but not confirmed running — run `squawk doctor` "
          "to diagnose.", file=sys.stderr)
    return 1


def cmd_uninstall():
    """Unload the agent, remove the app and LaunchAgent plist, and list the TCC
    entries the user may revoke (macOS keeps privacy grants after uninstall)."""
    r = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                       capture_output=True, text=True)
    print(f"Unloaded agent {AGENT_LABEL}." if r.returncode == 0
          else f"Agent {AGENT_LABEL} was not loaded.")

    removed = []
    for path, remover in ((AGENT_PLIST_DST, os.remove), (APP_DST, lambda p: shutil.rmtree(p, ignore_errors=True))):
        if os.path.exists(path):
            try:
                remover(path)
                removed.append(path)
            except OSError as exc:
                print(f"squawk: could not remove {path}: {exc}", file=sys.stderr)
    for p in removed:
        print(f"Removed {p}")
    if not removed:
        print("No SquawkPTT app or LaunchAgent plist found — nothing to remove.")

    print("\nmacOS keeps the privacy grants after uninstall. To revoke them, open")
    print("each pane and remove the SquawkPTT entry:")
    print(f"  Microphone:       {PANE_MIC}")
    print(f"  Accessibility:    {PANE_AX}")
    print(f"  Input Monitoring: {PANE_INPUT}")
    print("\nYour config (~/.config/squawk/config) is left in place; delete it "
          "manually to remove the saved backend and key.")
    return 0


def check_clang():
    if shutil.which("clang"):
        return True, "clang present"
    r = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return True, f"Xcode Command Line Tools at {r.stdout.strip()}"
    return False, "no clang / Xcode Command Line Tools — run: xcode-select --install"


def read_helper_log_status():
    """Derive SquawkPTT's own TCC state from the tail of its log. Parsing the log
    is the only reliable way to read ANOTHER process's per-app grants — the
    public TCC API only answers for the calling process. The helper logs, at each
    launch, its microphone authorization, whether Accessibility is trusted, and
    whether the event tap came up (which additionally requires Input Monitoring).
    Iterating in order and overwriting yields the most recent launch's values."""
    try:
        with open(HELPER_LOG) as f:
            tail = f.readlines()[-200:]
    except OSError:
        return None
    status: "dict[str, bool | None]" = {"mic": None, "ax": None, "tap": None}
    for line in tail:
        if "mic access granted" in line:
            status["mic"] = True
        elif "mic access DENIED" in line or "mic access previously denied" in line:
            status["mic"] = False
        elif "mic authorization status at launch:" in line:
            code = line.rstrip().rsplit(":", 1)[-1].strip()
            if code == "3":      # AVAuthorizationStatusAuthorized
                status["mic"] = True
            elif code == "2":    # Denied
                status["mic"] = False
        if "accessibility trusted: yes" in line:
            status["ax"] = True
        elif "accessibility trusted: no" in line:
            status["ax"] = False
        if "event tap active" in line:
            status["tap"] = True
        elif "could not create event tap" in line:
            status["tap"] = False
    return status


def check_automation_iterm():
    """Return (ok, message, determinable). Tests whether this process may drive
    iTerm via AppleScript (the Automation grant delivery needs). Undeterminable
    if iTerm is not running — we don't launch it just to probe."""
    if not shutil.which("osascript"):
        return False, "osascript missing", False
    if subprocess.run(["pgrep", "-x", "iTerm2"], capture_output=True).returncode != 0:
        return False, "iTerm2 not running — start iTerm and re-run to test Automation", False
    r = subprocess.run(["osascript", "-e", 'tell application "iTerm2" to get name'],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return True, "can control iTerm2", True
    if "-1743" in r.stderr or "not allow" in r.stderr.lower() or "not authoriz" in r.stderr.lower():
        return False, "not authorized to control iTerm2", True
    return False, r.stderr.strip() or "could not control iTerm2", True


def warn_legacy_karabiner():
    """Print a warning (never fatal) if an old hold-Space Karabiner rule lingers."""
    if not os.path.exists(KARABINER_JSON):
        return
    try:
        with open(KARABINER_JSON) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    for profile in data.get("profiles", []):
        for rule in profile.get("complex_modifications", {}).get("rules", []):
            if _rule_is_legacy(rule):
                print(f"[WARN] Karabiner: a legacy hold-Space rule is still in {KARABINER_JSON}.")
                print("         It is no longer needed and fights the native tap — "
                      "remove it with: squawk migrate")
                return


def cmd_doctor(cfg):
    """One-stop preflight: dependencies, the build toolchain, the LaunchAgent,
    SquawkPTT's permissions, Automation, and the speech backend. Prints the exact
    Settings pane for each failing item and exits non-zero if any required check
    fails."""
    print("squawk doctor — preflight checklist\n")
    state = {"failed": False}

    def report(status, label, msg, pane=None, required=True):
        print(f"[{status:<4}] {label}: {msg}")
        if pane:
            print(f"         open: {pane}")
        if required and status == "FAIL":
            state["failed"] = True

    # Dependencies + build toolchain.
    sox = shutil.which("sox") or (SOX if os.access(SOX, os.X_OK) else None)
    report("OK" if sox else "FAIL", "sox", sox or "not found — install with: brew install sox")

    clang_ok, clang_msg = check_clang()
    report("OK" if clang_ok else "FAIL", "build toolchain", clang_msg)

    # Agent lifecycle.
    if agent_running():
        report("OK", "LaunchAgent", f"{AGENT_LABEL} loaded and running")
    else:
        report("FAIL", "LaunchAgent", f"{AGENT_LABEL} not running — run: squawk install-agent")

    # The CLI the helper actually spawns. Everything else can be green while this
    # is broken — the agent runs, permissions are granted, the backend answers —
    # and holding Space still does nothing, because the helper cannot find the
    # binary to run. Check the recorded path, not merely that some squawk exists.
    cli, cli_src = helper_cli_path()
    if cli is None:
        report("FAIL", "CLI reachable by helper",
               "the LaunchAgent records no SQUAWK_CLI and no squawk was found in "
               "~/bin, /opt/homebrew/bin, or /usr/local/bin — push-to-talk cannot "
               "start. Fix: squawk install-agent")
    elif not os.access(cli, os.X_OK):
        report("FAIL", "CLI reachable by helper",
               f"{cli} ({cli_src}) is missing or not executable — push-to-talk "
               f"cannot start. Fix: squawk install-agent")
    else:
        report("OK", "CLI reachable by helper", f"{cli} ({cli_src})")

    # SquawkPTT's own per-app grants, read from its launch log.
    st = read_helper_log_status()
    if st is None:
        report("WARN", "SquawkPTT permissions",
               "no helper log yet — run `squawk install-agent`, trigger it once, then re-run",
               required=False)
    else:
        if st["mic"] is True:
            report("OK", "Microphone (SquawkPTT)", "granted")
        elif st["mic"] is False:
            report("FAIL", "Microphone (SquawkPTT)", "not granted", PANE_MIC)
        else:
            report("WARN", "Microphone (SquawkPTT)", "unknown from log", PANE_MIC, required=False)

        ax = st["ax"] if st["ax"] is not None else st["tap"]
        if ax is True:
            report("OK", "Accessibility (SquawkPTT)", "granted")
        elif ax is False:
            report("FAIL", "Accessibility (SquawkPTT)", "not granted — hold-to-talk stays off", PANE_AX)
        else:
            report("WARN", "Accessibility (SquawkPTT)", "unknown from log", PANE_AX, required=False)

        if st["tap"] is True:
            report("OK", "Input Monitoring (SquawkPTT)", "event tap active")
        elif st["tap"] is False:
            report("FAIL", "Input Monitoring (SquawkPTT)",
                   "event tap did not start — grant Accessibility and Input Monitoring", PANE_INPUT)
        else:
            report("WARN", "Input Monitoring (SquawkPTT)", "unknown from log", PANE_INPUT, required=False)

    # Automation (iTerm) — only meaningful when iTerm is running.
    auto_ok, auto_msg, determinable = check_automation_iterm()
    if determinable:
        report("OK" if auto_ok else "FAIL", "Automation (iTerm)", auto_msg,
               None if auto_ok else PANE_AUTOMATION)
    else:
        report("WARN", "Automation (iTerm)", auto_msg, PANE_AUTOMATION, required=False)

    # Mode indicator — informational only, never a reason to fail: dictation
    # works fine without it.
    if cfg.indicator == "off":
        report("OK", "Mode indicator", "disabled (SQUAWK_INDICATOR=off)", required=False)
    else:
        report("OK", "Mode indicator", f"{cfg.indicator} — if a glyph or cursor colour "
               f"ever gets stuck, run: squawk reset-indicator", required=False)

    # Speech backend (verify_backend prints its own [endpoint]/[roundtrip] lines).
    print("\n[backend]")
    if not verify_backend(cfg):
        state["failed"] = True

    print()
    warn_legacy_karabiner()

    print()
    if state["failed"]:
        print("Some required checks failed — fix the FAIL items above, then re-run `squawk doctor`.")
        return 1
    print("All required checks passed. squawk is ready.")
    return 0


BACKEND_PRESETS = (
    ("OpenAI", "https://api.openai.com/v1", "whisper-1",
     "hosted; needs a key from https://platform.openai.com/api-keys"),
    ("Groq", "https://api.groq.com/openai/v1", "whisper-large-v3",
     "hosted; needs a key from https://console.groq.com/keys"),
    ("Custom / local", None, None,
     "any OpenAI-compatible server (whisper.cpp, faster-whisper, LM Studio, a LAN gateway)"),
)


def ask(prompt, default="", secret=False):
    """Read one answer. Interactively this prompts on the terminal (hiding keys);
    with piped stdin it consumes one line, so `setup` is scriptable and testable."""
    suffix = f" [{default}]" if default else ""
    interactive = sys.stdin.isatty()
    # stdout is block-buffered when piped, so flush the menu/notes printed above
    # before prompting — otherwise a scripted run's transcript is out of order.
    sys.stdout.flush()
    try:
        if secret and interactive:
            answer = getpass.getpass(f"{prompt}{suffix}: ")
        else:
            if not interactive:
                # The prompt still goes to stderr so a piped run stays readable.
                print(f"{prompt}{suffix}: ", end="", file=sys.stderr, flush=True)
                answer = sys.stdin.readline()
                if not answer:
                    raise EOFError
                print(answer.strip() if not secret else "***", file=sys.stderr)
            else:
                answer = input(f"{prompt}{suffix}: ")
    except EOFError:
        print("", file=sys.stderr)
        return default
    answer = unquote(answer)
    return answer or default


def write_config_file(updates, path=CONFIG_PATH):
    """Persist `updates` into the config file, preserving any other settings
    already there (tunables the user hand-edited). Directory 700, file 600 —
    it holds an API key."""
    existing = load_config_file(path, warn=False)
    existing.update(updates)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.chmod(os.path.dirname(path), 0o700)
    except OSError:
        pass
    lines = ["# squawk configuration — KEY=value, one per line.",
             "# Environment variables of the same name override these.", ""]
    for key in SETTING_KEYS:
        if key in existing:
            lines.append(f"{key}={existing.pop(key)}")
    for key in sorted(existing):
        lines.append(f"{key}={existing[key]}")
    body = "\n".join(lines) + "\n"

    # Create with 600 from the start (not chmod-after) so the key is never
    # briefly readable by other users.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.chmod(path, 0o600)


def cmd_setup():
    print("squawk setup — choose a speech-to-text backend.\n")

    legacy_key = ""
    if is_legacy_bare_key_config():
        legacy_key = load_config_file(CONFIG_PATH, warn=False).get("SQUAWK_SPEECH_KEY", "")
        print(f"Found a legacy bare-key config at {CONFIG_PATH} (just an API key, no "
              f"settings).\nsetup can rewrite it in the current KEY=value format, "
              f"keeping that key.")
        if ask("Reuse the existing key? (y/n)", "y").lower().startswith("y"):
            print("  → keeping the existing key; press Enter at the key prompt to accept it.\n")
        else:
            legacy_key = ""
            print("")

    for i, (name, url, model, note) in enumerate(BACKEND_PRESETS, start=1):
        target = f"{url} ({model})" if url else "you supply the URL and model"
        print(f"  {i}) {name} — {target}\n     {note}")
    print("")

    choice = ask("Backend", "1")
    try:
        index = int(choice) - 1
        name, url, model, _ = BACKEND_PRESETS[index]
        if index < 0:
            raise IndexError
    except (ValueError, IndexError):
        print(f"squawk: not a listed backend: {choice!r}", file=sys.stderr)
        return 2

    if url is None:
        url = ask("OpenAI-compatible base URL (e.g. http://127.0.0.1:8080/v1)")
        if not url:
            print("squawk: a base URL is required for a custom backend", file=sys.stderr)
            return 2
        model = ask("Model id", DEFAULT_STT_MODEL)
    else:
        model = ask(f"Model id for {name}", model or DEFAULT_STT_MODEL)

    if name == "OpenAI":
        print("  note: OpenAI bills Whisper per minute of audio (~$0.006/min). A local\n"
              "        server (option 3) is free and keeps audio on this machine.")
    key_prompt = "API key (leave blank for a keyless local server)"
    key = ask(key_prompt, legacy_key, secret=True)

    write_config_file({
        "SQUAWK_SPEECH_URL": url.rstrip("/"),
        "SQUAWK_SPEECH_KEY": key,
        "SQUAWK_STT_MODEL": model,
    })
    print(f"\nWrote {CONFIG_PATH} (mode 600).\n")

    # Verify before the user walks away, so a bad key surfaces here and not
    # mid-dictation. The mic is deliberately not probed: it has its own TCC
    # failure mode and `squawk check` covers it.
    for key_name in ("SQUAWK_SPEECH_URL", "SQUAWK_SPEECH_KEY", "SQUAWK_STT_MODEL"):
        if key_name in os.environ:
            print(f"squawk: note: ${key_name} is exported and overrides the config file.",
                  file=sys.stderr)
    cfg = load_config()
    print("Verifying the backend:")
    if not verify_backend(cfg):
        print(f"\nVerification failed. The config file is written — fix it by re-running "
              f"`squawk setup` or editing {CONFIG_PATH}.", file=sys.stderr)
        return 1
    print("\nBackend verified. Run `squawk check` to also test the microphone.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="squawk", description="Voice dictation for the terminal")
    sub = parser.add_subparsers(dest="command", required=True)

    dictate_p = sub.add_parser("dictate", help="record until silence, transcribe, and print the transcript")
    dictate_p.add_argument("--keep-audio", action="store_true", help="do not delete the captured WAV file")
    dictate_p.add_argument("--keep-newlines", action="store_true", help="do not collapse internal newlines")

    sub.add_parser("ptt-start", help="begin push-to-talk recording in the background (returns immediately)")

    ptt_stop_p = sub.add_parser("ptt-stop", help="stop push-to-talk recording; transcribe and inject the transcript in the background")
    ptt_stop_p.add_argument("--keep-audio", action="store_true", help="do not delete the captured WAV file")
    ptt_stop_p.add_argument("--keep-newlines", action="store_true", help="do not collapse internal newlines")

    sub.add_parser("setup", help="choose a speech backend interactively and save it to ~/.config/squawk/config")

    sub.add_parser("check", help="verify mic capture and speech endpoint health")

    sub.add_parser("migrate", help="disable the legacy Karabiner hold-Space rule and agent (reversibly) before using the native tap")

    sub.add_parser("install-agent", help="build/place SquawkPTT.app and load its LaunchAgent")

    sub.add_parser("uninstall", help="unload and remove the SquawkPTT agent, app, and LaunchAgent plist")

    sub.add_parser("doctor", help="check every prerequisite (deps, permissions, backend, agent) and print fixes")

    sub.add_parser("reset-indicator", help="clear a stuck mode indicator (badge and cursor colour) from the current session")

    # Internal: the detached transcription+delivery worker spawned by ptt-stop.
    # No help= so it stays out of the documented command list.
    finish_p = sub.add_parser("_finish")
    finish_p.add_argument("wav")
    finish_p.add_argument("--session", default="")
    finish_p.add_argument("--pid", type=int, default=0)
    finish_p.add_argument("--tty", default="")
    finish_p.add_argument("--keep-audio", action="store_true")
    finish_p.add_argument("--keep-newlines", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "setup":
        # Loaded after setup writes the file, not before.
        return cmd_setup()
    if args.command == "migrate":
        # Legacy-teardown only; no speech config needed.
        return cmd_migrate()
    if args.command == "install-agent":
        # Agent lifecycle only; no speech config needed.
        return cmd_install_agent()
    if args.command == "uninstall":
        return cmd_uninstall()

    cfg = load_config()

    if args.command == "doctor":
        return cmd_doctor(cfg)
    if args.command == "reset-indicator":
        return cmd_reset_indicator(cfg)

    if args.command == "dictate":
        return cmd_dictate(cfg, keep_audio=args.keep_audio, keep_newlines=args.keep_newlines)
    if args.command == "ptt-start":
        return cmd_ptt_start(cfg)
    if args.command == "ptt-stop":
        return cmd_ptt_stop(cfg, keep_audio=args.keep_audio, keep_newlines=args.keep_newlines)
    if args.command == "_finish":
        return cmd_finish(cfg, args.wav, args.session, pid=args.pid, tty=args.tty or None,
                          keep_audio=args.keep_audio, keep_newlines=args.keep_newlines)
    return cmd_check(cfg)


if __name__ == "__main__":
    sys.exit(main())
