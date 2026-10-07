"""The catalogs that ship in ``po/`` but Hungarian (``test_i18n_hu.py``): each is registered,
complete, and shown by the programs in a session of that language.

The other i18n tests write their own catalogs; these read the real ones. The first group needs no
gettext (it reads the text of the files). The second compiles the real catalogs with ``msgfmt`` and
skips without it, with the name of the package (the CI installs it, so there a skip fails the job).
The tests that start ``msgfmt`` / ``tools/i18n.sh`` are listed in ``OPT_OUTS`` of
``test_tripwire.py``.

German, Italian, French and Spanish are written out below (``LANGUAGES``: what a session in the
language shows is typed here, so a wrong catalog cannot vouch for itself). The other languages are
read from their own catalog (``expected_strings``): for those the tests check that the catalog is
whole, consistent and wired in, which is all they can check. ``test_i18n_catalogs.py`` has the
checks that are the same for every language (header, plural rule, glossary).

Nobody who speaks these languages natively has read the translations yet; the tests check that they
are there, whole and wired in, not that they are good German, Italian, French, Spanish, Dutch ...
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
from tests.i18n_catalogs import (  # noqa: E402
    FIRST_FIVE,
    HELD_BY,
    WITHOUT_DATA_STRINGS,
    data_msgids,
    header_fields,
    parse_po,
    shipped,
)

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "slideshow_lock"
PO = REPO / "po"

_spec = importlib.util.spec_from_file_location("i18n_catalog", REPO / "tools" / "i18n_catalog.py")
catalog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(catalog)

#: Per language: the session language, then what a session in that language shows. The first three
#: are strings of the window, ``In use`` is filled with a folder name, the last four are texts of
#: ``--help`` (preview_app, service and preferences description, and the ``--debug`` help).
LANGUAGES = {
    "de": {
        "session": "de_DE.UTF-8",
        "Cancel": "Abbrechen",
        "Preview": "Vorschau",
        "In use: %s": "In Verwendung: %s",
        "help": (
            (preview_app, "Die Vorschau der Diashow anzeigen"),
            (service, "Startet die Diashow"),
            (preferences, "Die Einstellungen der Diashow ändern"),
        ),
        "debug": "jeden Schritt protokollieren",
    },
    "it": {
        "session": "it_IT.UTF-8",
        "Cancel": "Annulla",
        "Preview": "Anteprima",
        "In use: %s": "In uso: %s",
        "help": (
            (preview_app, "Mostrare l'anteprima della presentazione"),
            (service, "Avviare la presentazione quando"),
            (preferences, "Modificare le impostazioni della presentazione"),
        ),
        "debug": "registrare ogni passaggio",
    },
    "fr": {
        "session": "fr_FR.UTF-8",
        "Cancel": "Annuler",
        "Preview": "Aperçu",
        "In use: %s": "En cours d’utilisation : %s",
        "help": (
            (preview_app, "Affiche l’aperçu du diaporama"),
            (service, "Démarre le diaporama"),
            (preferences, "Modifier les paramètres du diaporama"),
        ),
        "debug": "journaliser chaque étape",
    },
    "es": {
        "session": "es_ES.UTF-8",
        "Cancel": "Cancelar",
        "Preview": "Vista previa",
        "In use: %s": "En uso: %s",
        "help": (
            (preview_app, "Mostrar la vista previa de la presentación"),
            (service, "Iniciar la presentación cuando"),
            (preferences, "Cambiar las preferencias de la presentación"),
        ),
        "debug": "registrar cada paso",
    },
}

#: What the tests read from a catalog that is not written out in ``LANGUAGES``: the strings of the
#: window and the three ``--help`` descriptions (preview_app, service, preferences), as msgids.
HELP_MSGIDS = (
    (preview_app, "Show the slideshow preview. Any input ends it. It never locks the session."),
    (
        service,
        "Start the slideshow when the session is idle, lock it on the first input after the grace "
        "period, and lock before the machine suspends.",
    ),
    (preferences, "Change the slideshow settings."),
)
DEBUG_MSGID = "log every step"

#: Every catalog these tests look at: the four written out above, and the other shipped languages
#: but Hungarian (its own test file).
ALL_LANGUAGES = sorted(set(LANGUAGES) | {lang for lang in shipped() if lang != "hu"})

#: The languages of the negative control (it copies the checkout and runs ``tools/i18n.sh``, so
#: not every language: the mechanism is the same for all of them).
CONTROL_LANGUAGES = sorted(set(LANGUAGES) | ({"ja", "pt_BR"} & set(ALL_LANGUAGES)))

AI_TRANSLATOR = "AI-assisted (Claude), not reviewed by a native speaker"
HUMAN_TRANSLATOR = "Slideshow Lock contributors"

#: Strings that are the same as the English on purpose: the name of the program and the units
#: (``10 s``, ``%d min``, ``1 h`` are written the same in all of these languages), and a word that
#: is the same in French (``Transitions``), the name of a transition that is a person's name
#: (``Ken Burns``) or a loan word that most of these languages write as it is (``Zoom``).
SAME_AS_ENGLISH = {
    "Slideshow Lock",
    "Transitions",
    "Ken Burns",
    "Zoom",
    "10 s",
    "1 min",
    "1 h",
    "24 h",
    "%d s",
    "%d min",
    "%d h",
}
PLACEHOLDER = re.compile(r"%(?:\([A-Za-z_]+\))?[sdif]|%%")


def source_msgids():
    return {call.msgid for call in catalog.gettext_calls(PACKAGE)}


def expected_msgids(lang):
    """What the catalog of *lang* holds: the strings of the source (without those that only some
    catalogs hold yet, ``HELD_BY``) and, for every catalog but the first five
    (``WITHOUT_DATA_STRINGS``), the five of the launcher and the metadata."""
    own = source_msgids() - {msgid for msgid, langs in HELD_BY.items() if lang not in langs}
    if lang in WITHOUT_DATA_STRINGS:
        return own
    return own | data_msgids()


def expected_strings(lang):
    """What a session in *lang* shows: written out in ``LANGUAGES``, or the catalog's own text for
    the same msgids (the session is the language name: ``pt_BR.UTF-8``, ``nl.UTF-8``)."""
    if lang in LANGUAGES:
        return LANGUAGES[lang]
    texts = dict(entries(lang)[0])
    return {
        "session": lang + ".UTF-8",
        "Cancel": texts["Cancel"],
        "Preview": texts["Preview"],
        "In use: %s": texts["In use: %s"],
        "help": tuple((module, texts[msgid]) for module, msgid in HELP_MSGIDS),
        "debug": texts[DEBUG_MSGID],
    }


def po_path(lang):
    return PO / (lang + ".po")


def linguas(text):
    return [name for line in text.splitlines() for name in line.split("#")[0].split()]


def entries(lang):
    """``(msgid, msgstr)`` of every entry of ``po/<lang>.po`` but the header, and the raw lines."""
    lines = po_path(lang).read_text(encoding="utf-8").split("\n")
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


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_the_language_is_in_linguas_and_has_its_catalog(lang):
    assert lang in linguas((PO / "LINGUAS").read_text(encoding="utf-8"))
    assert po_path(lang).is_file()


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_the_catalog_holds_exactly_the_strings_of_the_source_and_every_one_is_translated(lang):
    pairs, _lines = entries(lang)
    msgids = [msgid for msgid, _msgstr in pairs]
    assert len(msgids) == len(set(msgids)), "an entry twice"
    assert set(msgids) == expected_msgids(lang)
    assert [msgid for msgid, msgstr in pairs if msgstr == ""] == []
    english = {msgid for msgid, msgstr in pairs if msgstr == msgid}
    assert english <= SAME_AS_ENGLISH, sorted(english - SAME_AS_ENGLISH)
    assert "Slideshow Lock" in english


def test_a_string_held_by_some_catalogs_is_held_by_exactly_those():
    """``HELD_BY`` names the catalogs that have a string: those have it, every other one lacks it
    (a catalog that gets the translation must be added to ``HELD_BY``, not left out of it)."""
    for msgid, holders in HELD_BY.items():
        assert msgid in source_msgids(), "%r is not asked for by the source any more" % msgid
        assert holders <= set(shipped()), sorted(holders - set(shipped()))
        for lang in shipped():
            has = msgid in {m for m, _s in entries(lang)[0]}
            assert has == (lang in holders), "%s: %s" % (lang, "has it" if has else "lacks it")


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_no_entry_is_fuzzy_or_obsolete(lang):
    _pairs, lines = entries(lang)
    assert [line for line in lines if line.startswith("#,") and "fuzzy" in line] == []
    assert [line for line in lines if line.startswith("#~")] == []


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_the_placeholders_and_the_commands_are_unchanged(lang):
    pairs, _lines = entries(lang)
    for msgid, msgstr in pairs:
        assert Counter(PLACEHOLDER.findall(msgstr)) == Counter(PLACEHOLDER.findall(msgid)), msgid
        for line in msgid.split("\n"):
            if line.startswith("  "):  # a command the user copies: glib-compile-schemas ...
                assert line in msgstr.split("\n"), msgid
        assert msgstr.endswith("\n") == msgid.endswith("\n"), msgid
        assert msgstr.count("\n") == msgid.count("\n"), msgid


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_the_file_has_unix_line_ends_and_a_header_that_names_no_person(lang):
    raw = po_path(lang).read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    text = raw.decode("utf-8")
    assert '"Content-Type: text/plain; charset=UTF-8\\n"\n' in text
    assert '"Language: %s\\n"\n' % lang in text
    translator = HUMAN_TRANSLATOR if lang in FIRST_FIVE else AI_TRANSLATOR
    assert header_fields(parse_po(text)[0])["Last-Translator"] == translator
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


def build(checkout, target):
    return subprocess.run(
        ["bash", str(checkout / "tools" / "i18n.sh"), "build", str(target)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.spawns_processes
@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_msgfmt_counts_every_message_translated_none_fuzzy_none_untranslated(gettext_tools, lang):
    result = subprocess.run(
        ["msgfmt", "--statistics", "-c", "--check-format", "-o", "/dev/null", str(po_path(lang))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.strip() == "%d translated messages." % len(expected_msgids(lang))


@pytest.fixture(scope="module")
def built_locale(tmp_path_factory):
    """Every catalog of ``po/LINGUAS`` compiled once by ``tools/i18n.sh build``, for the module."""
    if shutil.which("msgfmt") is None:
        pytest.skip("msgfmt is not on PATH (package gettext); the CI installs it")
    target = tmp_path_factory.mktemp("locale")
    built = build(REPO, target)
    assert built.returncode == 0, built.stderr
    return target


@pytest.mark.spawns_processes
@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_the_catalog_built_from_this_checkout_is_what_the_programs_show_in_its_language(
    gettext_tools, restore_gettext_state, built_locale, monkeypatch, capsys, lang
):
    expected = expected_strings(lang)
    assert (built_locale / lang / "LC_MESSAGES" / (APP_ID + ".mo")).is_file()

    for name in i18n.LANGUAGE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", expected["session"])
    monkeypatch.setenv(i18n.LOCALEDIR_ENV, str(built_locale))
    monkeypatch.setenv("COLUMNS", "300")  # argparse must not wrap a text it is compared with
    i18n.setup()
    assert _("Cancel") == expected["Cancel"]
    assert _("Preview") == expected["Preview"]
    assert _("In use: %s") % "x" == expected["In use: %s"] % "x"
    assert _("Cancel") != "Cancel"
    # a string this catalog does not hold yet (HELD_BY) is the English text, not an error
    assert _("Show screenshots") == "Show screenshots"

    for module, text in expected["help"]:
        with pytest.raises(SystemExit) as stopped:
            module.main(["--help"])
        assert stopped.value.code == 0
        out = capsys.readouterr().out
        assert text in out and expected["debug"] in out, module.__name__


@pytest.mark.spawns_processes
@pytest.mark.parametrize("lang", CONTROL_LANGUAGES)
def test_a_language_that_is_not_in_linguas_is_not_built_and_the_check_fails(
    gettext_tools, tmp_path, lang
):
    """Negative control: the tests above are not satisfied by a catalog that is only lying about."""
    checkout = tmp_path / "checkout"
    for name in ("tools", "slideshow_lock", "po"):
        shutil.copytree(
            REPO / name, checkout / name, ignore=shutil.ignore_patterns("__pycache__", "*.mo")
        )
    text = (PO / "LINGUAS").read_text(encoding="utf-8")
    kept = [name for name in linguas(text) if name != lang]
    assert kept != linguas(text)
    (checkout / "po" / "LINGUAS").write_text("\n".join(kept) + "\n", encoding="utf-8")

    checked = subprocess.run(
        ["bash", str(checkout / "tools" / "i18n.sh"), "check"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert checked.returncode != 0
    assert lang in checked.stderr

    built = build(checkout, tmp_path / "locale")
    assert not (tmp_path / "locale" / lang).exists(), built.stderr
