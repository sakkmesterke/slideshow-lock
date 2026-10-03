#!/bin/bash
# Run the preview smoke test inside a headless mutter with virtual monitors.
#
#   tools/wayland-smoke/run.sh [--monitors 1280x720,800x1000] [smoke_preview.py arguments]
#
# Needs: mutter (with --headless), dbus-run-session, glib-compile-schemas, and PyGObject with
# GTK 4 and GStreamer (gir1.2-gtk-4.0, gir1.2-gst-plugins-base-1.0, gstreamer1.0-plugins-base).
# Not run by pytest or CI: it needs a compositor. Prints one "SMOKE ..." line per check and
# exits non-zero if any check failed.
#
# MUTTER_ARGS (extra mutter options) and DBUS_ARGS (extra dbus-run-session options) are
# passed through for unusual installs.
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
dbus-run-session ${DBUS_ARGS:-} -- "$HERE/inner.sh" "$@"
