#!/usr/bin/env bash
# Try slideshow-lock from a source checkout: no installation, nothing written into the checkout.
#
#   ./run.sh check              look for the dependencies, install nothing
#   ./run.sh preview [args...]  the fullscreen preview (never locks), args go to preview_app
#   ./run.sh settings           the settings window
#   ./run.sh service [args...]  the whole chain in the foreground (idle, slideshow, lock), args go
#                               to slideshow_lock.service, e.g. --idle-timeout 20 --grace 3
#
# The settings schema is compiled into ${XDG_CACHE_HOME:-$HOME/.cache}/slideshow-lock/schemas.
set -euo pipefail

src="${BASH_SOURCE[0]}"
case "$src" in
    */*) dir="${src%/*}" ;;
    *) dir="." ;;
esac
REPO="$(cd -- "$dir" && pwd)"

usage() {
    cat <<'EOF'
usage: ./run.sh check              look for the dependencies (installs nothing)
       ./run.sh preview [args...]  fullscreen preview, e.g. --interval 5 (never locks)
       ./run.sh settings           the settings window
       ./run.sh service [args...]  idle, slideshow and lock in the foreground, e.g.
                                   --idle-timeout 20 --grace 3 (Ctrl+C stops it)
EOF
}

die() {
    printf 'run.sh: %s\n' "$1" >&2
    exit 1
}

MISSING=0

# report_missing WHAT WHY PACKAGE
report_missing() {
    MISSING=$((MISSING + 1))
    printf 'MISSING: %s\n' "$1" >&2
    printf '         %s\n' "$2" >&2
    printf '         likely dnf package, not verified on RHEL 10.2: %s\n' "$3" >&2
}

report_ok() {
    if [ "${VERBOSE:-0}" = 1 ]; then
        printf 'ok:      %s\n' "$1"
    fi
}

# typelib NAME VERSION: can PyGObject load that typelib?
typelib() {
    python3 -c 'import sys
import gi
gi.require_version(sys.argv[1], sys.argv[2])' "$1" "$2" >/dev/null 2>&1
}

# check_typelib NAME VERSION PACKAGE WHAT
check_typelib() {
    if typelib "$1" "$2"; then
        report_ok "$4 ($1-$2 typelib)"
    else
        report_missing "$4" "PyGObject cannot load the $1-$2 typelib." "$3"
    fi
}

# Looks for everything the preview, the settings window and the service load. VERBOSE=1 prints the ok lines.
do_check() {
    MISSING=0

    if command -v python3 >/dev/null 2>&1; then
        if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
            report_ok "python3 ($(python3 -c 'import sys; print(sys.version.split()[0])'))"
        else
            report_missing "python3 3.9 or newer" "python3 is on the PATH but older than 3.9." "python3"
        fi
        if python3 -c 'import gi' >/dev/null 2>&1; then
            report_ok "gi (PyGObject)"
            check_typelib Gtk 4.0 gtk4 "GTK 4"
            check_typelib Gdk 4.0 gtk4 "Gdk 4"
            check_typelib Graphene 1.0 graphene "Graphene"
            check_typelib GdkPixbuf 2.0 gdk-pixbuf2 "GdkPixbuf (picture decoding)"
            check_typelib Gio 2.0 glib2 "Gio"
        else
            report_missing "gi (PyGObject)" "python3 cannot import gi, so no typelib could be checked." "python3-gobject"
        fi
    else
        report_missing "python3" "no python3 on the PATH; the GTK typelibs were not checked." "python3"
    fi

    if command -v glib-compile-schemas >/dev/null 2>&1; then
        report_ok "glib-compile-schemas"
    else
        report_missing "glib-compile-schemas" "needed to compile the settings schema." "glib2-devel"
    fi

    if [ -n "${WAYLAND_DISPLAY:-}" ]; then
        report_ok "Wayland session (WAYLAND_DISPLAY=$WAYLAND_DISPLAY)"
    else
        printf 'MISSING: Wayland session\n' >&2
        printf '         WAYLAND_DISPLAY is not set; run this from a terminal inside the desktop session.\n' >&2
        MISSING=$((MISSING + 1))
    fi

    # Optional: the preview falls back to GdkPixbuf bilinear scaling without it (one WARNING).
    if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)
sys.exit(0 if Gst.ElementFactory.find("videoscale") else 1)' >/dev/null 2>&1; then
        report_ok "GStreamer videoscale (Lanczos scaling)"
    else
        printf 'WARNING: GStreamer with the videoscale element was not found (optional).\n' >&2
        printf '         Pictures are then scaled bilinear instead of Lanczos.\n' >&2
        printf '         likely dnf package, not verified on RHEL 10.2: gstreamer1-plugins-base\n' >&2
    fi

    if [ "$MISSING" -gt 0 ]; then
        printf '\n%d required item(s) missing. The package names above are likely names, not verified on RHEL 10.2.\n' "$MISSING" >&2
        return 1
    fi
    return 0
}

# Compiles the schema outside the checkout and points GSettings and Python at the right places.
prepare_env() {
    local base cache
    if [ -n "${XDG_CACHE_HOME:-}" ]; then
        base="$XDG_CACHE_HOME"
    elif [ -n "${HOME:-}" ]; then
        base="$HOME/.cache"
    else
        die "neither XDG_CACHE_HOME nor HOME is set; cannot choose a place for the compiled schema"
    fi
    cache="$base/slideshow-lock/schemas"

    if ! compgen -G "$REPO/data/*.gschema.xml" >/dev/null; then
        die "no data/*.gschema.xml in $REPO; run this script from a complete checkout"
    fi
    mkdir -p -- "$cache" || die "cannot create $cache"
    cp -- "$REPO"/data/*.gschema.xml "$cache"/ || die "cannot copy the schema into $cache"
    glib-compile-schemas "$cache" || die "glib-compile-schemas failed on $cache"

    export GSETTINGS_SCHEMA_DIR="$cache${GSETTINGS_SCHEMA_DIR:+:$GSETTINGS_SCHEMA_DIR}"
    export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
}

case "${1:-}" in
    check)
        VERBOSE=1
        do_check
        ;;
    preview)
        shift
        do_check || exit 1
        prepare_env
        exec python3 -m slideshow_lock.preview_app "$@"
        ;;
    settings)
        if [ ! -f "$REPO/slideshow_lock/preferences.py" ]; then
            die "the settings window is not available yet in this checkout"
        fi
        do_check || exit 1
        prepare_env
        exec python3 -m slideshow_lock.preferences
        ;;
    service)
        shift
        do_check || exit 1
        prepare_env
        exec python3 -m slideshow_lock.service "$@"
        ;;
    -h | --help | help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
