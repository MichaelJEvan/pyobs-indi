#!/bin/bash
###############################################################################################
#
#   Michael J Evan
#   MS Computer Science | University of Massachusetts Dartmouth 2026
#   AAVSO (American Association of Variable Star Observers)
#
#   Re-attach the mount's USB to the OrbStack indi VM after a power-cycle.
#
#   Mac development-machine tool only. OrbStack does not hand a re-enumerated
#   USB device back to the VM by itself: after a mount power-cycle the serial
#   node is gone from the VM until someone runs orb usb detach/attach (board
#   issue #13). On the bare-metal Linux box the kernel recreates the node on
#   its own and this problem does not exist -- this script retires with the
#   Mac setup.
#
#   What it does, once per poll: if the VM already has the serial node, nothing.
#   If the node is missing and macOS does not see the device, the mount is off
#   or unplugged -- nothing to do but wait. If the node is missing while macOS
#   DOES see the device, run the recovery: best-effort detach, settle, attach,
#   verify. The pyobs-indi module then reconnects and re-syncs site/time on
#   its own (verified live 2026-09-03).
#
#   The recovery order is shaped by two measured failures (both 2026-09-03):
#   - detach can be REQUIRED (a stale "attached" claim blocks the new attach)
#     or can FAIL with "invalid USB port" (the registry entry is gone), so it
#     is best-effort and never gates the attach -- a `detach && attach` chain
#     silently skipped the attach.
#   - attach straight after power-on refuses with "not attachable" while
#     macOS is still enumerating; a few seconds of settle clears it.
#
#   Run in its own terminal window:  ./tools/orb_usb_watch.sh
#   Stop with Ctrl-C. Safe to leave running: a healthy link is never touched.
#
###############################################################################################

set -u

# Pin the window title; without this the terminal titles the pane after the
# current foreground process, which is mostly "sleep".
printf '\e]0;7 USB-WATCH\a'

USB_ID="${USB_ID:-02100000}"                  # orb usb list shows it if it changes
MACHINE="${MACHINE:-indi}"                    # the OrbStack VM running indiserver
BY_ID="${BY_ID:-usb-ZWO_Systems_ZWO_Device_123456-if00}"   # the driver's saved port

POLL=5            # seconds between checks. A choice, not a measurement: small
                  # next to the module's 60 s slow-retry cadence, so the node
                  # is back well before the next reconnect attempt.
SETTLE=5          # wait between detach and attach. Attach right after
                  # power-on refused with "not attachable" (2026-09-03);
                  # enumeration takes a few seconds.
VERIFY_WAIT=10    # seconds to wait for the node to appear after an attach
BACKOFF=30        # after a failed recovery, slow down instead of hammering

log() { printf '%s  orb-usb-watch  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

ORB_DEADLINE=15   # seconds an orb command may take before it is presumed hung

# Run one orb command with a deadline. The orb CLI can hang forever (measured
# 2026-09-08: one `orb ls` sat 15h47m and froze the whole loop -- a frozen
# watcher is worse than none, because it looks alive). On timeout the command
# is killed, the hang is logged to stderr (stdout may be feeding a pipe), and
# the call reports failure; the loop carries on to the next poll.
orb_dl() {
    "$@" &
    local cmd=$!
    ( sleep "$ORB_DEADLINE"; kill "$cmd" 2>/dev/null ) &
    local dog=$!
    wait "$cmd"
    local rc=$?
    kill "$dog" 2>/dev/null
    wait "$dog" 2>/dev/null
    # orb can leave the terminal's newline handling scrambled (stair-stepped
    # log lines, seen 2026-09-07 once orb ran under this wrapper); restore
    # the settings after every call so the window stays readable.
    [ -t 0 ] && stty sane 2>/dev/null
    if [ "$rc" -ge 128 ]; then
        log "orb call hung past ${ORB_DEADLINE}s and was killed: $*" >&2
    fi
    return "$rc"
}

vm_has_node() {
    orb_dl orb -m "$MACHINE" ls /dev/serial/by-id/ 2>/dev/null | grep -q "$BY_ID"
}

mac_sees_device() {
    orb_dl orb usb list 2>/dev/null | grep -q "^$USB_ID"
}

# Log on state changes only, like the module's edge-triggered warnings --
# silence while healthy or while the mount stays off is correct, not a stall.
last_state=""
note_state() {
    if [ "$1" != "$last_state" ]; then
        log "$2"
        last_state="$1"
    fi
}

recover() {
    log "device present on the Mac but no serial node in the '$MACHINE' VM; re-attaching"
    # Best-effort: the stale-attach case needs this, the gone-from-registry
    # case errors on it. Either way the attach must still run.
    orb_dl orb usb detach "$USB_ID" >/dev/null 2>&1
    sleep "$SETTLE"
    if ! orb_dl orb usb attach -m "$MACHINE" "$USB_ID"; then
        log "attach refused; backing off ${BACKOFF}s"
        sleep "$BACKOFF"
        return
    fi
    waited=0
    while [ "$waited" -lt "$VERIFY_WAIT" ]; do
        if vm_has_node; then
            log "serial node restored; the module reconnects on its own"
            return
        fi
        sleep 1
        waited=$((waited + 1))
    done
    log "attached but no node after ${VERIFY_WAIT}s; backing off ${BACKOFF}s"
    sleep "$BACKOFF"
}

log "watching USB $USB_ID for the '$MACHINE' VM (Ctrl-C stops)"
while true; do
    if vm_has_node; then
        note_state healthy "serial node present; watching"
    elif mac_sees_device; then
        last_state=""
        recover
    else
        note_state absent "mount off or unplugged; waiting"
    fi
    sleep "$POLL"
done
