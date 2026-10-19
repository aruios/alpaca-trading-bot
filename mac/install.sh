#!/bin/bash
# Install the Mac runner as a launch agent: starts at login, restarts if it stops,
# and keeps the Mac awake (caffeinate) while it runs. Undo with mac/uninstall.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.aruios.tradingbot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -f "$ROOT/.env" ] || { echo "Create $ROOT/.env first with ALPACA_API_KEY=... and ALPACA_SECRET_KEY=... (paper keys)"; exit 1; }
command -v gh >/dev/null || { echo "Install the GitHub CLI first: brew install gh"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Log in to GitHub first: gh auth login"; exit 1; }

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "Creating .venv ..."
  python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/pip" install -q --upgrade pip
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
mkdir -p "$ROOT/logs" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-is</string>
    <string>$ROOT/.venv/bin/python</string><string>$ROOT/runner.py</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$(dirname "$(command -v gh)"):/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$ROOT/logs/runner.out</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/runner.out</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Runner installed and started. Follow it with: tail -f $ROOT/logs/\$(date +%F).log"
