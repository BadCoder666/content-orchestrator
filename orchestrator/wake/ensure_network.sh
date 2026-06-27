#!/bin/bash
# ensure_network.sh — make sure the Mac has internet before the overnight scrape
# / morning poll. Safe to call repeatedly (no-op when already online). Callable
# headless by BOTH the root wake-armer (00:46) and the user-agent dispatcher.
#
# Strategy, cheapest first:
#   1. Already online?            → done (also covers Ethernet/USB-tether: if any
#                                   interface has internet we never touch Wi-Fi).
#   2. Power Wi-Fi on, let macOS AUTO-JOIN the top preferred network in range
#      (home "your home Wi-Fi", else the phone hotspot "My Phone").  → recheck.
#   3. Still down? EXPLICITLY join each fallback SSID in order (uses the saved
#      Keychain PSK; reliable from the USER-agent caller whose login keychain is
#      unlocked. The phone hotspot must be BROADCASTING — networksetup cannot wake
#      an iPhone's Personal Hotspot via Instant Hotspot).
#
# Exit 0 if online by the end, 1 if not (caller logs/alerts; never aborts the run).
#
# Usage: ensure_network.sh [iface] [SSID ...]   (defaults: en0 "My Phone" "My Phone 5G")
set -u
NS=/usr/sbin/networksetup

# Resolve the ACTUAL Wi-Fi device (en0 isn't Wi-Fi on every Mac / docked setups).
IFACE="$("$NS" -listallhardwareports 2>/dev/null | awk '/Wi-Fi|AirPort/{getline; print $2; exit}')"
[ -z "${IFACE:-}" ] && IFACE="${1:-en0}"
shift || true
SSIDS=("$@"); [ ${#SSIDS[@]} -eq 0 ] && SSIDS=("My Phone" "My Phone 5G")

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') ensure_net: $*"; }

online() {
    # Authoritative first: TLS to a raw IP — can't be captive-portal-spoofed and
    # needs no DNS. -4 avoids a black-holed IPv6 route. 8s for slow hotspots.
    curl -4 -fs -m 8 -o /dev/null "https://1.1.1.1" 2>/dev/null && return 0
    # Secondary: Apple captive check — a captive portal returns its OWN 200, so we
    # must see the literal "Success" body, not just a 2xx status.
    curl -4 -fs -m 8 "http://captive.apple.com/hotspot-detect.html" 2>/dev/null \
        | grep -qi "Success" && return 0
    return 1
}

wait_online() {  # $1 = tries (×3s)
    local n="$1"
    for _ in $(seq 1 "$n"); do
        online && return 0
        sleep 3
    done
    return 1
}

if online; then
    log "internet OK (no action; iface=$IFACE)"
    exit 0
fi

log "no internet — powering Wi-Fi ($IFACE) on, waiting for auto-join"
"$NS" -setairportpower "$IFACE" on 2>/dev/null || true
if wait_online 8; then
    cur="$("$NS" -getairportnetwork "$IFACE" 2>/dev/null | sed 's/^.*: //')"
    log "recovered via auto-join (${cur:-unknown})"
    exit 0
fi

for SSID in "${SSIDS[@]}"; do
    log "auto-join failed — explicitly joining \"$SSID\""
    "$NS" -setairportnetwork "$IFACE" "$SSID" 2>/dev/null || true
    if wait_online 8; then
        log "recovered via \"$SSID\""
        exit 0
    fi
done

log "FAILED to obtain internet (Wi-Fi up but no network reachable; phone hotspot may be off)"
exit 1
