#!/bin/bash
# Root wake-armer / disarmer for the overnight batch. Installed as a root
# LaunchDaemon (com.example.claude-wake-armer) that fires TWICE via two
# StartCalendarInterval entries:
#   • 00:46 — ARM:    ensure internet, keep the Mac awake (disablesleep), arm the
#                     backstop + Window-B wakes.
#   • 02:00 — DISARM: restore normal sleep.
#
# WHY two invocations instead of one blocking script: the keep-awake must be
# released even if the arm run is SIGKILLed (an EXIT trap does NOT fire on
# SIGKILL → the Mac would never sleep again). A separate disarm invocation always
# clears it; and every arm run also clears any stale hold up front. No process
# blocks for an hour.
#
# WHY disablesleep, not caffeinate: the lid is CLOSED at night → 'Clamshell
# Sleep', which `caffeinate -i` cannot stop. `pmset -a disablesleep 1` keeps the
# system awake lid-closed; `-a` = ALL power sources (battery AND AC) — the Mac is
# often on battery overnight, so AC-only would be a no-op.
#
# Keychain note: the EXPLICIT hotspot join in ensure_network.sh reads the Wi-Fi
# PSK from a keychain. Run as root here it sees the System keychain; the saved
# home/hotspot PSKs usually live in the user's LOGIN keychain, so the reliable joiner
# is the USER-agent dispatcher (which also calls ensure_network on every dark
# tick). This root run mainly powers Wi-Fi on so macOS can AUTO-join.
set -u
PMSET=/usr/bin/pmset
HERE="$(cd "$(dirname "$0")" && pwd)"
TODAY="$(date "+%m/%d/%y")"
NOWHM=$((10#$(date +%H%M)))   # HHMM as base-10 int (avoids octal on leading 0)

# DISARM invocation (~02:00): restore sleep and exit. Also the catch-all that
# heals a hold left set by a SIGKILLed arm run from earlier tonight.
if [ "$NOWHM" -ge 150 ] && [ "$NOWHM" -le 210 ]; then
    "$PMSET" -a disablesleep 0 2>/dev/null || true
    echo "$(date '+%Y-%m-%d %H:%M:%S') DISARM: disablesleep=0 (normal sleep restored)"
    exit 0
fi

# ARM invocation (~00:46) — or a stray RunAtLoad. Always clear any stale hold
# first (self-heal), then bring the network up and arm wakes (both idempotent).
"$PMSET" -a disablesleep 0 2>/dev/null || true
"$HERE/ensure_network.sh" en0 "My Phone" "My Phone 5G" 2>&1 || true

WAKES="01:05:00 01:20:00 01:45:00 06:00:00 06:30:00 07:00:00 07:30:00 08:00:00 08:30:00"
for t in $WAKES; do "$PMSET" schedule cancel wake "$TODAY $t" 2>/dev/null || true; done
for t in $WAKES; do "$PMSET" schedule wake "$TODAY $t" || true; done
echo "$(date '+%Y-%m-%d %H:%M:%S') armed Window-A backstop + Window-B wakes for $TODAY"

# Set the keep-awake hold ONLY during the real Window-A arm window (guards against
# a RunAtLoad/boot at a random hour triggering an all-day hold). The 02:00 DISARM
# invocation releases it.
if [ "$NOWHM" -ge 40 ] && [ "$NOWHM" -le 110 ]; then
    if "$PMSET" -a disablesleep 1 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') ARM: disablesleep=1 all-power (held until 02:00 disarm)"
    fi
fi
exit 0
