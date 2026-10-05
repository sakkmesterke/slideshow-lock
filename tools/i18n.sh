#!/usr/bin/env bash
# The translation work flow. The interface text in the source is English (_("...")); a language is
# one catalog po/<lang>.po plus its name in po/LINGUAS, no code changes. Needs gettext
# (xgettext, msgmerge, msgfmt) and python3.
#
#   tools/i18n.sh extract [FILE]   write the template (default po/messages.pot, not in git)
#   tools/i18n.sh update           extract, then merge the template into every catalog
#   tools/i18n.sh build DIR        compile the catalogs into DIR/<lang>/LC_MESSAGES/<APP_ID>.mo
#   tools/i18n.sh check            catalogs and po/LINGUAS agree, every catalog compiles, the
#                                  template holds exactly the strings the source asks for
set -euo pipefail

src="${BASH_SOURCE[0]}"
case "$src" in
    */*) dir="${src%/*}" ;;
    *) dir="." ;;
esac
REPO="$(cd -- "$dir/.." && pwd)"
PO="$REPO/po"

usage() {
    sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

die() {
    printf 'i18n.sh: %s\n' "$1" >&2
    exit 1
}

need() {
    command -v "$1" >/dev/null 2>&1 || die "$1 not found (package: gettext)"
}

# The gettext domain is APP_ID, the one constant in slideshow_lock/__init__.py.
domain() {
    python3 -c 'import sys
sys.path.insert(0, sys.argv[1])
from slideshow_lock import APP_ID
print(APP_ID)' "$REPO"
}

# The languages of po/LINGUAS, one per line.
linguas() {
    [ -f "$PO/LINGUAS" ] || die "$PO/LINGUAS is missing"
    sed 's/#.*//' "$PO/LINGUAS" | tr -s ' \t' '\n\n' | { grep -v '^$' || true; }
}

extract_to() {
    local out=$1
    need xgettext
    mkdir -p -- "$(dirname -- "$out")"
    (
        cd -- "$REPO"
        find slideshow_lock -name '*.py' | LC_ALL=C sort \
            | xargs xgettext --language=Python --from-code=UTF-8 --keyword=_ \
                --package-name=slideshow-lock --add-location=file -o "$out"
    )
}

do_build() {
    local out=$1 lang name
    need msgfmt
    name="$(domain)"
    mkdir -p -- "$out"
    for lang in $(linguas); do
        [ -f "$PO/$lang.po" ] || die "po/LINGUAS names $lang, but po/$lang.po does not exist"
        mkdir -p -- "$out/$lang/LC_MESSAGES"
        msgfmt -c --check-format -o "$out/$lang/LC_MESSAGES/$name.mo" "$PO/$lang.po"
    done
}

do_check() {
    local lang files listed failed=0
    need msgfmt
    need xgettext

    files="$(cd -- "$PO" && find . -maxdepth 1 -name '*.po' | sed 's|^\./||; s|\.po$||' | LC_ALL=C sort)"
    listed="$(linguas | LC_ALL=C sort -u)"
    if [ "$files" != "$listed" ]; then
        printf 'i18n: po/LINGUAS and the po/*.po files differ:\n' >&2
        printf '  catalogs: %s\n  LINGUAS:  %s\n' "$(echo $files)" "$(echo $listed)" >&2
        failed=1
    fi
    for lang in $files; do
        msgfmt -c --check-format -o /dev/null "$PO/$lang.po" || failed=1
        # msginit writes the charset of the shell's locale (ASCII under C), not UTF-8.
        if ! grep -q '^"Content-Type: text/plain; charset=UTF-8' "$PO/$lang.po"; then
            printf 'i18n: po/%s.po: the header must say charset=UTF-8\n' "$lang" >&2
            failed=1
        fi
    done

    TMP="$(mktemp -d)"
    trap 'rm -rf -- "$TMP"' EXIT
    extract_to "$TMP/messages.pot"
    python3 "$REPO/tools/i18n_catalog.py" compare "$TMP/messages.pot" "$REPO/slideshow_lock" || failed=1
    return "$failed"
}

case "${1:-}" in
    extract)
        extract_to "${2:-$PO/messages.pot}"
        ;;
    update)
        need msgmerge
        extract_to "$PO/messages.pot"
        for lang in $(linguas); do
            msgmerge --update --backup=none --quiet "$PO/$lang.po" "$PO/messages.pot"
        done
        ;;
    build)
        [ -n "${2:-}" ] || die "build needs the directory to write to"
        do_build "$2"
        ;;
    check)
        do_check
        ;;
    -h | --help | help)
        usage
        ;;
    *)
        usage
        exit 2
        ;;
esac
