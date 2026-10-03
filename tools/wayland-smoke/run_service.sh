#!/bin/bash
# Run the service smoke test: the real `python3 -m slideshow_lock.service` in a headless mutter.
#
#   tools/wayland-smoke/run_service.sh [--monitors 1280x720,800x1000]
#
# Same needs as run.sh, plus dbus-daemon. Mutter provides the real idle monitor, the real
# windows open on its virtual monitors, and pointer motion is injected through its
# remote-desktop service. What mutter does not provide is faked here, on its session bus and on
# a second private bus standing in for the system bus: the screensaver, the session manager and
# login1 (``tests/fake_dbus.py``). Not part of pytest or CI.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export SMOKE_MONITORS="1280x720,800x1000"
if [ "${1:-}" = "--monitors" ]; then SMOKE_MONITORS="$2"; shift 2; fi

SMOKE_WORK="$(mktemp -d)"
export SMOKE_WORK
trap 'rm -rf "$SMOKE_WORK"' EXIT
mkdir -p "$SMOKE_WORK/schemas" "$SMOKE_WORK/run"
chmod 700 "$SMOKE_WORK/run"
cp "$REPO"/data/*.gschema.xml "$SMOKE_WORK/schemas/"
glib-compile-schemas "$SMOKE_WORK/schemas" || exit 2

export XDG_RUNTIME_DIR="$SMOKE_WORK/run"
export GSETTINGS_BACKEND=memory
export GSETTINGS_SCHEMA_DIR="$SMOKE_WORK/schemas${GSETTINGS_SCHEMA_DIR:+:$GSETTINGS_SCHEMA_DIR}"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
# shellcheck disable=SC2086
dbus-run-session ${DBUS_ARGS:-} -- "$HERE/inner_service.sh" "$@"
