#!/bin/bash
# Started by run_service.sh inside a private D-Bus session: mutter, then the service smoke script.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ARGS=()
for size in ${SMOKE_MONITORS//,/ }; do ARGS+=(--virtual-monitor "$size"); done
# shellcheck disable=SC2086
mutter --headless --wayland --no-x11 "${ARGS[@]}" --wayland-display=smoke-0 ${MUTTER_ARGS:-} \
    > "$SMOKE_WORK/mutter.log" 2>&1 &
MUTTER_PID=$!
for _ in $(seq 1 100); do [ -S "$SMOKE_WORK/run/smoke-0" ] && break; sleep 0.1; done
export WAYLAND_DISPLAY=smoke-0 GDK_BACKEND=wayland GTK_A11Y=none
python3 -u "$HERE/smoke_service.py" "$@"
STATUS=$?
kill "$MUTTER_PID" 2>/dev/null
wait "$MUTTER_PID" 2>/dev/null
exit "$STATUS"
