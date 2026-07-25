#!/bin/sh
# Build SquawkPTT.app, install it to ~/Applications, and (re)load its LaunchAgent.
# See squawkptt.m for why this helper exists (it owns the CGEventTap that drives
# hold-to-talk and holds the mic/Accessibility TCC grants).
set -eu

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/SquawkPTT.app"
AGENT_SRC="$SRC_DIR/sh.squawk.ptt.plist"
AGENT_DST="$HOME/Library/LaunchAgents/sh.squawk.ptt.plist"
LABEL="sh.squawk.ptt"

mkdir -p "$HOME/Applications" "$HOME/bin" "$HOME/Library/Logs"

# ~/bin/squawk must be a real local COPY, not a symlink into the repo: the repo
# may live on a removable volume, and SquawkPTT (a background agent) blocks
# forever in open() waiting on the Removable Volumes TCC consent it can never
# show.
cp "$SRC_DIR/../squawk.py" "$HOME/bin/squawk"
chmod +x "$HOME/bin/squawk"

# Stop a running instance before overwriting the binary.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

# Compile + ad-hoc-sign the app via the shared build step (the same one the
# Homebrew formula and `squawk install-agent` use).
sh "$SRC_DIR/build.sh" "$APP"

mkdir -p "$HOME/Library/LaunchAgents"
# The plist ships with __HOME__ placeholders (it can't expand ~ or $HOME
# itself); substitute the real home so Program and the log path are absolute.
sed "s|__HOME__|$HOME|g" "$AGENT_SRC" > "$AGENT_DST"
launchctl bootstrap "gui/$(id -u)" "$AGENT_DST"
launchctl print "gui/$(id -u)/$LABEL" | grep -E 'state|pid' | head -3
echo "SquawkPTT installed and running."
