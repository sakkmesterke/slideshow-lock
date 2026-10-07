"""The Hungarian catalog that ships in ``po/hu.po``: it is registered, it is complete, and the
programs show it.

The other i18n tests write their own catalogs; these read the real one. The first group needs no
gettext (it reads the text of the file). The second compiles the real catalog with ``msgfmt`` and
skips without it, with the name of the package (the CI installs it, so there a skip fails the job).
The two tests that start ``msgfmt`` / ``tools/i18n.sh`` are listed in ``OPT_OUTS`` of
``test_tripwire.py``.

Nobody who speaks Hungarian natively has read the translation yet; the tests check that it is
there, whole and wired in, not that it is good Hungarian.
"""

from __future__ import annotations

import ast
import gettext
import importlib.util
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import gi
import pytest

gi.require_version("Gtk", "4.0")

from slideshow_lock import APP_ID, _, i18n, preferences, preview_app, service  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "slideshow_lock"
PO = REPO / "po"
HU_PO = PO / "hu.po"

_spec = importlib.util.spec_from_file_location("i18n_catalog", REPO / "tools" / "i18n_catalog.py")
catalog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(catalog)

#: Strings that are the same in Hungarian on purpose: the name of the program and the name of
#: the transition that is a person's name.
SAME_AS_ENGLISH = {"Slideshow Lock", "Ken Burns"}
PLACEHOLDER = re.compile(r"%(?:\([A-Za-z_]+\))?[sdif]|%%")


def source_msgids():
    return {call.msgid for call in catalog.gettext_calls(PACKAGE)}


def entries():
    """``(msgid, msgstr)`` of every entry of ``po/hu.po`` except the header, and the raw lines."""
    lines = HU_PO.read_text(encoding="utf-8").split("\n")
    pairs, field, current = [], None, {"msgid": "", "msgstr": ""}
    for line in lines + ['msgid ""']:
        if line.startswith("msgid "):
            if field is not None:
                pairs.append((current["msgid"], current["msgstr"]))
            current, field = {"msgid": ast.literal_eval(line[6:]), "msgstr": ""}, "msgid"
        elif line.startswith("msgstr "):
            current["msgstr"], field = ast.literal_eval(line[7:]), "msgstr"
        elif line.startswith('"') and field is not None:
            current[field] += ast.literal_eval(line)
    return [(msgid, msgstr) for msgid, msgstr in pairs if msgid], lines


def test_hungarian_is_in_linguas_and_has_its_catalog():
    listed = [
        name
        for line in (PO / "LINGUAS").read_text(encoding="utf-8").splitlines()
        for name in line.split("#")[0].split()
    ]
    assert "hu" in listed
    assert HU_PO.is_file()


def test_the_catalog_holds_exactly_the_strings_of_the_source_and_every_one_is_translated():
    pairs, _lines = entries()
    msgids = [msgid for msgid, _msgstr in pairs]
    assert len(msgids) == len(set(msgids)), "an entry twice"
    assert set(msgids) == source_msgids()
    untranslated = [msgid for msgid, msgstr in pairs if msgstr == ""]
    assert untranslated == []
    english = {msgid for msgid, msgstr in pairs if msgstr == msgid}
    assert english == SAME_AS_ENGLISH


def test_no_entry_is_fuzzy_or_obsolete():
    _pairs, lines = entries()
    assert [line for line in lines if line.startswith("#,") and "fuzzy" in line] == []
    assert [line for line in lines if line.startswith("#~")] == []


def test_the_placeholders_and_the_commands_are_unchanged():
    pairs, _lines = entries()
    for msgid, msgstr in pairs:
        assert Counter(PLACEHOLDER.findall(msgstr)) == Counter(PLACEHOLDER.findall(msgid)), msgid
        for line in msgid.split("\n"):
            if line.startswith("  "):  # a command the user copies: glib-compile-schemas ...
                assert line in msgstr.split("\n"), msgid
        assert msgstr.endswith("\n") == msgid.endswith("\n"), msgid
        assert msgstr.count("\n") == msgid.count("\n"), msgid


def test_the_file_has_unix_line_ends_and_a_header_that_names_no_person_or_agent():
    raw = HU_PO.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    text = raw.decode("utf-8")
    assert '"Content-Type: text/plain; charset=UTF-8\\n"\n' in text
    assert '"Language: hu\\n"\n' in text
    assert '"Last-Translator: Slideshow Lock contributors\\n"\n' in text
    assert "@" not in text.split("\n\n", 1)[0]


# -- with gettext ------------------------------------------------------------------------------


@pytest.fixture
def gettext_tools():
    for tool in ("msgfmt", "xgettext"):
        if shutil.which(tool) is None:
            pytest.skip("%s is not on PATH (package gettext); the CI installs it" % tool)


@pytest.fixture
def restore_gettext_state():
    """``setup()`` changes the process-wide text domain; the other tests get it back."""
    domain, localedir = gettext.textdomain(), gettext.bindtextdomain(APP_ID)
    yield
    gettext.textdomain(domain)
    gettext.bindtextdomain(APP_ID, localedir)


@pytest.mark.spawns_processes
def test_msgfmt_counts_every_message_translated_none_fuzzy_none_untranslated(gettext_tools):
    result = subprocess.run(
        ["msgfmt", "--statistics", "-c", "--check-format", "-o", "/dev/null", str(HU_PO)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.strip() == "%d translated messages." % len(source_msgids())


@pytest.mark.spawns_processes
def test_the_catalog_built_from_this_checkout_is_what_the_programs_show_in_a_hungarian_session(
    gettext_tools, restore_gettext_state, tmp_path, monkeypatch, capsys
):
    built = subprocess.run(
        ["bash", str(REPO / "tools" / "i18n.sh"), "build", str(tmp_path / "locale")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    assert (tmp_path / "locale" / "hu" / "LC_MESSAGES" / (APP_ID + ".mo")).is_file()

    for name in i18n.LANGUAGE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "hu_HU.UTF-8")
    monkeypatch.setenv(i18n.LOCALEDIR_ENV, str(tmp_path / "locale"))
    i18n.setup()
    assert _("Cancel") == "Mégse"
    assert _("Preview") == "Előnézet"
    assert _("%d min") % 5 == "5 perc"
    assert _("Show screenshots") == "Képernyőképek megjelenítése"

    for module, text in (
        (preview_app, "A diavetítés előnézetének megjelenítése"),
        (service, "Elindítja a diavetítést"),
        (preferences, "A diavetítés beállításainak módosítása"),
    ):
        with pytest.raises(SystemExit) as stopped:
            module.main(["--help"])
        assert stopped.value.code == 0
        out = capsys.readouterr().out
        assert text in out and "minden lépés naplózása" in out, module.__name__
