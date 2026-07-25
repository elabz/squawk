#!/bin/sh
# squawk installer for users without Homebrew. Installs the CLI and stages the
# helper build inputs where `squawk install-agent` looks for them, then hands
# off — mirroring what the Homebrew formula does.
#
#   curl -fsSL https://raw.githubusercontent.com/elabz/squawk/main/install.sh | sh
#
# Please read this script before piping it to sh. It writes only to ~/bin and
# ~/.local/share/squawk, builds nothing until you run `squawk install-agent`,
# and never asks for sudo.
set -eu

REPO_URL="https://github.com/elabz/squawk.git"
BIN_DIR="${SQUAWK_BIN_DIR:-$HOME/bin}"
SHARE_DIR="$HOME/.local/share/squawk/helper"

# Sources: run from a checkout if squawk.py sits beside us, else clone shallowly.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLEANUP=""
if [ -f "$SCRIPT_DIR/squawk.py" ] && [ -d "$SCRIPT_DIR/helper" ]; then
    SRC="$SCRIPT_DIR"
else
    command -v git >/dev/null 2>&1 || {
        echo "squawk: git is required to fetch sources over curl" >&2
        exit 1
    }
    SRC="$(mktemp -d)"
    CLEANUP="$SRC"
    echo "Cloning $REPO_URL …"
    git clone --depth 1 "$REPO_URL" "$SRC" >/dev/null 2>&1
fi

# 1. CLI — a real local copy (never a symlink into a possibly-removable volume;
#    the background helper can hang on open() waiting for Removable-Volume TCC).
mkdir -p "$BIN_DIR"
cp "$SRC/squawk.py" "$BIN_DIR/squawk"
chmod +x "$BIN_DIR/squawk"
echo "Installed squawk CLI -> $BIN_DIR/squawk"

# 2. Helper build inputs where `squawk install-agent` discovers them.
mkdir -p "$SHARE_DIR"
cp "$SRC/helper/squawkptt.m" "$SRC/helper/Info.plist" \
   "$SRC/helper/build.sh" "$SRC/helper/sh.squawk.ptt.plist" "$SHARE_DIR/"
chmod +x "$SHARE_DIR/build.sh"
echo "Staged helper sources -> $SHARE_DIR"

[ -n "$CLEANUP" ] && rm -rf "$CLEANUP"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not on your PATH — add it to your shell profile." ;;
esac

cat <<'EOS'

Installed. Next steps:
  squawk install-agent   # build SquawkPTT.app + load the LaunchAgent
  squawk setup           # choose your speech-to-text backend
  squawk doctor          # verify permissions and backend
EOS
