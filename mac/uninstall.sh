#!/bin/bash
# Stop the Mac runner and remove its launch agent. GitHub Actions takes over within ~45 min.
LABEL="com.aruios.tradingbot"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "Runner stopped." || echo "Runner was not running."
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
gh variable delete MAC_HEARTBEAT -R aruios/alpaca-trading-bot 2>/dev/null && echo "Heartbeat cleared, GitHub runs resume now." || true
