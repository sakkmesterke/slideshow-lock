"""``tools/i18n.sh`` with the real gettext tools, run on copies of the checkout.

gettext is a system package (``msgfmt``, ``xgettext``, ``msgmerge``). Without it these tests skip
with the name of the package; the CI installs it before pytest, so there a skip fails the job
(the no-skip gate). Every test starts a program on purpose, so each is listed in ``OPT_OUTS`` of
``test_tripwire.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
    """A copy of what ``tools/i18n.sh`` reads: the package, the tools, and an empty po/."""
    root = tmp_path / "checkout"
    shutil.copytree(
        REPO / "slideshow_lock",
        root / "slideshow_lock",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (root / "tools").mkdir()
    for name in ("i18n.sh", "i18n_catalog.py"):
        shutil.copy2(REPO / "tools" / name, root / "tools" / name)
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
