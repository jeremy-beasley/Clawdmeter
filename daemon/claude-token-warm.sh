#!/bin/bash
# Keep Claude Code's OAuth token fresh for the Clawdmeter daemon.
#
# The daemon never refreshes the token itself (see test_freeride.py) — Claude Code
# owns that, and only does it when it makes an API call. After a long idle stretch
# (laptop asleep, away from the desk) the token expires and the device shows
# "Token expired". launchd runs this on a timer (and on wake, as missed intervals
# fire once the Mac resumes) so Claude Code makes one tiny call and renews it.
#
# Cost: one Haiku turn with tools disabled, no session saved. It does count toward
# the 5h usage window, so keep the launchd interval modest.
LOG="${HOME}/Library/Logs/claude-token-warm.log"
CLAUDE="$(command -v claude || echo /opt/homebrew/bin/claude)"

cd "$HOME" || exit 1
# macOS has no `timeout`; perl's alarm bounds a hung call to 2 minutes. exec means
# $? is claude's own exit code (142 = killed by the alarm).
out=$(perl -e 'alarm 120; exec @ARGV' "$CLAUDE" -p "Reply with: ok" \
    --model haiku --no-session-persistence --tools "" </dev/null 2>&1)
rc=$?
if [ "$rc" -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ok exit=0" >> "$LOG"
else
    # Keep the first line of output so a failure (auth, network, rate limit) is diagnosable.
    echo "$(date '+%Y-%m-%d %H:%M:%S') FAILED exit=$rc: $(printf '%s' "$out" | head -n1 | cut -c1-200)" >> "$LOG"
fi
exit 0
