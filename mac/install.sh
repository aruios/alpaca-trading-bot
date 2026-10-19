#!/bin/bash
# Install the Mac runner as a launch agent: starts at login, restarts if it stops,
# and keeps the Mac awake (caffeinate) while it runs. Undo with mac/uninstall.sh
#
# The runner works from its own clone in ~/.alpaca-trading-bot, because macOS blocks
# background services from reading ~/Documents. runner.py pulls main before every run,
# so the clone stays current; re-run this script after changing .env.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
RUN_DIR="$HOME/.alpaca-trading-bot"
REPO_URL="https://github.com/aruios/alpaca-trading-bot.git"
LABEL="com.aruios.tradingbot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -f "$SRC/.env" ] || { echo "Create $SRC/.env first with ALPACA_API_KEY=... and ALPACA_SECRET_KEY=... (paper keys)"; exit 1; }
command -v gh >/dev/null || { echo "Install the GitHub CLI first: brew install gh"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Log in to GitHub first: gh auth login"; exit 1; }

if [ ! -d "$RUN_DIR/.git" ]; then
  echo "Cloning into $RUN_DIR ..."
  git -c credential.helper= -c credential.helper='!gh auth git-credential' clone -q "$REPO_URL" "$RUN_DIR"
fi
install -m 600 "$SRC/.env" "$RUN_DIR/.env"

if [ ! -x "$RUN_DIR/.venv/bin/python" ]; then
  echo "Creating $RUN_DIR/.venv ..."
  python3 -m venv "$RUN_DIR/.venv"
fi
"$RUN_DIR/.venv/bin/pip" install -q --upgrade pip
"$RUN_DIR/.venv/bin/pip" install -q -r "$RUN_DIR/requirements.txt"
mkdir -p "$RUN_DIR/logs" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-is</string>
    <string>$RUN_DIR/.venv/bin/python</string><string>$RUN_DIR/runner.py</string>
  </array>
  <key>WorkingDirectory</key><string>$RUN_DIR</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$(dirname "$(command -v gh)"):/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$RUN_DIR/logs/runner.out</string>
  <key>StandardErrorPath</key><string>$RUN_DIR/logs/runner.out</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Runner installed and started. Follow it with: tail -f $RUN_DIR/logs/\$(date +%F).log"
