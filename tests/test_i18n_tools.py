"""``tools/i18n.sh`` with the real gettext tools, run on copies of the checkout.

gettext is a system package (``msgfmt``, ``xgettext``, ``msgmerge``). Without it these tests skip
with the name of the package; the CI installs it before pytest, so there a skip fails the job
(the no-skip gate). Every test starts a program on purpose, so each is listed in ``OPT_OUTS`` of
``test_tripwire.py``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from slideshow_lock import APP_ID

pytestmark = pytest.mark.spawns_processes

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"

PO_HEADER = (
    'msgid ""\nmsgstr ""\n'
    '"Project-Id-Version: slideshow-lock\\n"\n'
    '"Language: hu\\n"\n'
    '"MIME-Version: 1.0\\n"\n'
    '"Content-Type: text/plain; charset=UTF-8\\n"\n'
    '"Content-Transfer-Encoding: 8bit\\n"\n'
)
HUNGARIAN_PO = PO_HEADER + '\nmsgid "Slideshow Lock"\nmsgstr "Diavetítés-zár"\n'


@pytest.fixture(autouse=True)
def gettext_tools():
    for tool in ("msgfmt", "xgettext", "msgmerge"):
        if shutil.which(tool) is None:
            pytest.skip("%s is not on PATH (package gettext); the CI installs it" % tool)


@pytest.fixture
def checkout(tmp_path):
    """A copy of what ``tools/i18n.sh`` reads: the package, the tools, the data templates, and an
    empty po/."""
    root = tmp_path / "checkout"
    shutil.copytree(
        REPO / "slideshow_lock",
        root / "slideshow_lock",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (root / "tools").mkdir()
    for name in ("i18n.sh", "i18n_catalog.py"):
        shutil.copy2(REPO / "tools" / name, root / "tools" / name)
    (root / "data").mkdir()
    for template in (REPO / "data").glob("*.in"):
        shutil.copy2(template, root / "data" / template.name)
    (root / "po").mkdir()
    (root / "po" / "LINGUAS").write_text("# nothing yet\n")
    return root


def _i18n(root, *args, env=None):
    return subprocess.run(
        [BASH, str(root / "tools" / "i18n.sh"), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )


def _catalog(root, name, text):
    (root / "po" / (name + ".po")).write_text(text, encoding="utf-8")


def test_check_passes_on_this_checkout():
    result = _i18n(REPO, "check")
    assert result.returncode == 0, result.stderr
    assert "distinct strings in the source" in result.stdout


def test_check_passes_with_a_catalog_that_is_listed(checkout):
    _catalog(checkout, "hu", HUNGARIAN_PO)
    (checkout / "po" / "LINGUAS").write_text("hu\n")
    result = _i18n(checkout, "check")
    assert result.returncode == 0, result.stderr


def test_check_fails_for_a_catalog_that_is_not_in_linguas(checkout):
    _catalog(checkout, "hu", HUNGARIAN_PO)
    result = _i18n(checkout, "check")
    assert result.returncode != 0
    assert "po/LINGUAS and the po/*.po files differ" in result.stderr
    assert "catalogs: hu" in result.stderr


def test_check_fails_for_a_language_in_linguas_without_a_catalog(checkout):
    (checkout / "po" / "LINGUAS").write_text("hu de\n")
    _catalog(checkout, "hu", HUNGARIAN_PO)
    result = _i18n(checkout, "check")
    assert result.returncode != 0
    assert "po/LINGUAS and the po/*.po files differ" in result.stderr


def test_check_fails_for_a_catalog_whose_format_directives_do_not_match(checkout):
    (checkout / "po" / "LINGUAS").write_text("hu\n")
    _catalog(
        checkout,
        "hu",
        PO_HEADER + '\n#, python-format\nmsgid "%d seconds"\nmsgstr "%s másodperc"\n',
    )
    result = _i18n(checkout, "check")
    assert result.returncode != 0
    assert "format specifications" in result.stderr


def test_check_fails_for_a_catalog_whose_charset_is_not_utf8(checkout):
    (checkout / "po" / "LINGUAS").write_text("hu\n")
    _catalog(checkout, "hu", HUNGARIAN_PO.replace("charset=UTF-8", "charset=ASCII"))
    result = _i18n(checkout, "check")
    assert result.returncode != 0
    assert "po/hu.po: the header must say charset=UTF-8" in result.stderr


def test_check_fails_for_a_string_the_extraction_cannot_see(checkout):
    (checkout / "slideshow_lock" / "added.py").write_text(
        "from slideshow_lock import _\n\n\ndef f(name):\n    return _(name)\n"
    )
    result = _i18n(checkout, "check")
    assert result.returncode != 0
    assert "plain string literal" in result.stderr


def test_build_writes_a_catalog_python_loads_under_the_domain_of_the_app(checkout, tmp_path):
    (checkout / "po" / "LINGUAS").write_text("hu\n")
    _catalog(checkout, "hu", HUNGARIAN_PO)
    out = tmp_path / "out"
    result = _i18n(checkout, "build", str(out))
    assert result.returncode == 0, result.stderr
    assert (out / "hu" / "LC_MESSAGES" / (APP_ID + ".mo")).is_file()

    env = dict(
        os.environ, LANGUAGE="hu", PYTHONPATH=str(checkout), SLIDESHOW_LOCK_LOCALEDIR=str(out)
    )
    shown = subprocess.run(
        [
            sys.executable,
            "-c",
            "from slideshow_lock import _, i18n\ni18n.setup()\nprint(_('Slideshow Lock'))",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert shown.stdout.strip() == "Diavetítés-zár", shown.stderr


def _mos(directory):
    return [p for p in directory.rglob("*.mo")] if directory.exists() else []


def test_build_rejects_a_catalog_whose_charset_is_not_utf8_and_leaves_no_catalog(
    checkout, tmp_path
):
    """Python's gettext stops at a charset it does not know; the build must not make that file."""
    (checkout / "po" / "LINGUAS").write_text("de hu\n")
    _catalog(checkout, "de", HUNGARIAN_PO)
    _catalog(checkout, "hu", HUNGARIAN_PO.replace("charset=UTF-8", "charset=CHARSET"))
    out = tmp_path / "out"
    result = _i18n(checkout, "build", str(out))
    assert result.returncode != 0
    assert "po/hu.po: the header must say charset=UTF-8" in result.stderr
    assert _mos(out) == []


@pytest.mark.parametrize("charset", ["UTF-8bogus", "UTF-8foo", "UTF-88", "UTF-8 "])
@pytest.mark.parametrize("command", ["build", "check"])
def test_a_charset_that_only_starts_with_utf8_is_refused(checkout, tmp_path, charset, command):
    """``charset=UTF-8bogus`` is a charset Python does not know: the whole line must match."""
    (checkout / "po" / "LINGUAS").write_text("hu\n")
    _catalog(checkout, "hu", HUNGARIAN_PO.replace("charset=UTF-8", "charset=" + charset))
    out = tmp_path / "out"
    result = _i18n(checkout, command, *((str(out),) if command == "build" else ()))
    assert result.returncode != 0
    assert "po/hu.po: the header must say charset=UTF-8" in result.stderr
    assert _mos(out) == []


@pytest.mark.parametrize("command", ["build", "check"])
def test_a_utf8_line_in_a_later_entry_does_not_make_the_header_utf8(checkout, tmp_path, command):
    """Only the header entry declares the charset; the same line further down is just text."""
    (checkout / "po" / "LINGUAS").write_text("hu\n")
    _catalog(
        checkout,
        "hu",
        PO_HEADER.replace("charset=UTF-8", "charset=ASCII")
        + '\nmsgid "x\\n"\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n',
    )
    out = tmp_path / "out"
    result = _i18n(checkout, command, *((str(out),) if command == "build" else ()))
    assert result.returncode != 0
    assert "po/hu.po: the header must say charset=UTF-8" in result.stderr
    assert _mos(out) == []


def test_a_catalog_without_a_header_entry_is_refused_even_with_the_line_in_an_entry(
    checkout, tmp_path
):
    (checkout / "po" / "LINGUAS").write_text("hu\n")
    _catalog(
        checkout,
        "hu",
        'msgid "x\\n"\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n',
    )
    out = tmp_path / "out"
    result = _i18n(checkout, "build", str(out))
    assert result.returncode != 0
    assert "po/hu.po: the header must say charset=UTF-8" in result.stderr
    assert _mos(out) == []


def test_the_header_is_found_behind_comments_and_among_other_header_lines(checkout, tmp_path):
    """The shape msginit and msgmerge write: translator comments, a flag, more header lines."""
    (checkout / "po" / "LINGUAS").write_text("hu\n")
    _catalog(
        checkout,
        "hu",
        "# Hungarian translation.\n#, fuzzy\n"
        + PO_HEADER
        + '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n'
        + '\nmsgid "Slideshow Lock"\nmsgstr "Diavetítés-zár"\n',
    )
    out = tmp_path / "out"
    result = _i18n(checkout, "build", str(out))
    assert result.returncode == 0, result.stderr
    assert len(_mos(out)) == 1


def test_a_build_that_fails_in_the_second_catalog_leaves_no_catalog_of_the_first(
    checkout, tmp_path
):
    (checkout / "po" / "LINGUAS").write_text("de hu\n")
    _catalog(checkout, "de", HUNGARIAN_PO)
    _catalog(
        checkout,
        "hu",
        PO_HEADER + '\n#, python-format\nmsgid "%d seconds"\nmsgstr "%s másodperc"\n',
    )
    out = tmp_path / "out"
    result = _i18n(checkout, "build", str(out))
    assert result.returncode != 0
    assert _mos(out) == []


@pytest.mark.parametrize(
    "name", ["../../escaped", "a/b", ".hidden", "..", "a;b", "*", "pt-BR", "-x"]
)
@pytest.mark.parametrize("command", ["build", "check", "update"])
def test_a_language_name_that_is_not_a_plain_name_is_refused(checkout, tmp_path, name, command):
    """The name is part of a path under po/ and under the output directory."""
    (checkout / "po" / "LINGUAS").write_text(name + "\n")
    (tmp_path / "escaped.po").write_text(HUNGARIAN_PO, encoding="utf-8")  # po/../../escaped.po
    (checkout / "po" / "other.po").write_text(HUNGARIAN_PO, encoding="utf-8")
    out = tmp_path / "deep" / "out"
    args = (str(out),) if command == "build" else ()
    result = _i18n(checkout, command, *args)
    assert result.returncode != 0
    assert '"%s" is not a language name' % name in result.stderr
    assert not (tmp_path / "escaped").exists()
    assert _mos(tmp_path / "deep") == []


@pytest.mark.parametrize("command", ["build", "check", "update"])
def test_a_name_with_a_hyphen_is_refused_with_the_name_gettext_looks_for(
    checkout, tmp_path, command
):
    """Python's gettext looks for pt_BR (LANG=pt_BR.UTF-8), never for a directory pt-BR."""
    (checkout / "po" / "LINGUAS").write_text("pt-BR\n")
    _catalog(checkout, "pt-BR", HUNGARIAN_PO)
    out = tmp_path / "out"
    result = _i18n(checkout, command, *((str(out),) if command == "build" else ()))
    assert result.returncode != 0
    assert (
        'po/LINGUAS: "pt-BR" is not a language name: gettext looks for pt_BR, use an underscore'
        in result.stderr
    )
    assert _mos(out) == []


def test_a_name_with_an_underscore_is_built_and_found_under_the_session_language(
    checkout, tmp_path
):
    (checkout / "po" / "LINGUAS").write_text("pt_BR\n")
    _catalog(checkout, "pt_BR", HUNGARIAN_PO.replace('"Language: hu', '"Language: pt_BR'))
    out = tmp_path / "out"
    result = _i18n(checkout, "build", str(out))
    assert result.returncode == 0, result.stderr
    env = dict(
        os.environ,
        LANGUAGE="",
        LC_ALL="pt_BR.UTF-8",
        PYTHONPATH=str(checkout),
        SLIDESHOW_LOCK_LOCALEDIR=str(out),
    )
    shown = subprocess.run(
        [
            sys.executable,
            "-c",
            "from slideshow_lock import _, i18n\ni18n.setup()\nprint(_('Slideshow Lock'))",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert shown.stdout.strip() == "Diavetítés-zár", shown.stderr


def test_build_stops_at_a_language_that_has_no_catalog(checkout, tmp_path):
    (checkout / "po" / "LINGUAS").write_text("hu\n")
    result = _i18n(checkout, "build", str(tmp_path / "out"))
    assert result.returncode != 0
    assert "po/hu.po does not exist" in result.stderr


def test_build_without_a_directory_says_so(checkout):
    result = _i18n(checkout, "build")
    assert result.returncode != 0
    assert "needs the directory" in result.stderr


def test_update_adds_the_strings_the_catalog_does_not_have_yet(checkout):
    (checkout / "po" / "LINGUAS").write_text("hu\n")
    _catalog(checkout, "hu", HUNGARIAN_PO)
    result = _i18n(checkout, "update")
    assert result.returncode == 0, result.stderr
    updated = (checkout / "po" / "hu.po").read_text(encoding="utf-8")
    assert 'msgstr "Diavetítés-zár"' in updated
    assert 'msgid "log every step"' in updated
    assert not list((checkout / "po").glob("*~"))


TRANSLATED_DATA_PO = (
    HUNGARIAN_PO
    + '\nmsgid "Set up the idle slideshow and the session lock"'
    + '\nmsgstr "A tétlen diavetítés beállítása"\n'
    + '\nmsgid "Idle slideshow screensaver that locks on input"'
    + '\nmsgstr "Tétlen diavetítés"\n'
)


def test_data_writes_the_launcher_and_the_metadata_with_the_translations(checkout, tmp_path):
    (checkout / "po" / "LINGUAS").write_text("hu\n")
    _catalog(checkout, "hu", TRANSLATED_DATA_PO)
    out = tmp_path / "out"
    result = _i18n(checkout, "data", str(out))
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in out.iterdir()) == [APP_ID + ".desktop", APP_ID + ".metainfo.xml"]

    desktop = (out / (APP_ID + ".desktop")).read_text(encoding="utf-8")
    assert "Name[hu]=Diavetítés-zár\n" in desktop
    assert "Comment[hu]=A tétlen diavetítés beállítása\n" in desktop
    assert "Exec=slideshow-lock settings\n" in desktop
    assert "Keywords[" not in desktop  # the keywords are not translated

    xml_lang = "{http://www.w3.org/XML/1998/namespace}lang"
    root = ET.parse(out / (APP_ID + ".metainfo.xml")).getroot()
    assert [e.text for e in root.findall("name") if e.get(xml_lang) == "hu"] == ["Diavetítés-zár"]
    assert [e.text for e in root.findall("summary") if e.get(xml_lang) == "hu"] == [
        "Tétlen diavetítés"
    ]
    assert root.findtext("id") == APP_ID  # what is not translated is copied as it is
    assert len(root.findall("description/p")) == 3  # untranslated paragraphs stay as they are


def test_data_without_a_catalog_writes_the_english_files(checkout, tmp_path):
    out = tmp_path / "out"
    result = _i18n(checkout, "data", str(out))
    assert result.returncode == 0, result.stderr
    desktop = (out / (APP_ID + ".desktop")).read_text(encoding="utf-8")
    assert "Name=Slideshow Lock\n" in desktop
    assert not [line for line in desktop.splitlines() if re.match(r"\w+\[", line)]


def test_data_leaves_no_file_behind_when_a_catalog_is_refused(checkout, tmp_path):
    (checkout / "po" / "LINGUAS").write_text("hu\n")
    _catalog(checkout, "hu", TRANSLATED_DATA_PO.replace("charset=UTF-8", "charset=ASCII"))
    out = tmp_path / "out"
    result = _i18n(checkout, "data", str(out))
    assert result.returncode != 0
    assert "po/hu.po: the header must say charset=UTF-8" in result.stderr
    assert not out.exists() or list(out.iterdir()) == []


def test_data_leaves_no_launcher_behind_when_the_metadata_fails(checkout, tmp_path):
    template = checkout / "data" / (APP_ID + ".metainfo.xml.in")
    template.write_text(template.read_text(encoding="utf-8").replace("</summary>", ""))
    out = tmp_path / "out"
    result = _i18n(checkout, "data", str(out))
    assert result.returncode != 0
    assert list(out.iterdir()) == []


def test_data_without_a_directory_says_so(checkout):
    result = _i18n(checkout, "data")
    assert result.returncode != 0
    assert "needs the directory" in result.stderr


@pytest.mark.parametrize("template", ["desktop.in", "metainfo.xml.in"])
@pytest.mark.parametrize("command", ["check", "extract"])
def test_a_missing_data_template_is_named(checkout, template, command):
    (checkout / "data" / (APP_ID + "." + template)).unlink()
    result = _i18n(checkout, command)
    assert result.returncode != 0
    assert (APP_ID + "." + template + " is missing") in result.stderr


def test_check_fails_for_a_metainfo_template_that_is_not_well_formed(checkout):
    template = checkout / "data" / (APP_ID + ".metainfo.xml.in")
    template.write_text(template.read_text(encoding="utf-8").replace("</summary>", ""))
    result = _i18n(checkout, "check")
    assert result.returncode != 0


def test_only_the_name_and_the_comment_of_the_launcher_are_handed_to_the_translators(
    checkout, tmp_path
):
    template = checkout / "data" / (APP_ID + ".desktop.in")
    template.write_text(template.read_text(encoding="utf-8") + "GenericName=A new name\n")
    pot = tmp_path / "messages.pot"
    result = _i18n(checkout, "extract", str(pot))
    assert result.returncode == 0, result.stderr
    text = pot.read_text(encoding="utf-8")
    assert 'msgid "Set up the idle slideshow and the session lock"' in text
    assert "A new name" not in text
    assert "screensaver;" not in text  # nor the keywords
