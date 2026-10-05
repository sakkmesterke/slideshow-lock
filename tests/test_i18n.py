"""The language of the interface: ``slideshow_lock.i18n`` and the three entry points.

The catalogs here are written by the test itself (``write_mo``, the format is a few lines), so
these tests need neither gettext nor a program: they fail without the change, they do not skip.
The real ``msgfmt`` is covered in ``test_i18n_tools.py`` and ``test_run_script.py``.

``conftest.py`` pins ``LANGUAGE=C``; each test names the language it wants.
"""

from __future__ import annotations

import gettext
import logging
import os
import struct
from array import array

import gi
import pytest

gi.require_version("Gtk", "4.0")

from gi.repository import Gio  # noqa: E402

from slideshow_lock import APP_ID, _, i18n, preferences, preview_app, service  # noqa: E402

HEADER = "Content-Type: text/plain; charset=UTF-8\n"
HUNGARIAN = {
    "": HEADER,
    "Slideshow Lock": "Diavetítés-zár",
    "log every step": "minden lépés naplózása",
}


def write_mo(localedir, lang, messages):
    """A compiled catalog, ``<localedir>/<lang>/LC_MESSAGES/<APP_ID>.mo`` (what msgfmt makes)."""
    keys = sorted(messages)
    ids = strs = b""
    offsets = []
    for key in keys:
        key_bytes, value_bytes = key.encode("utf-8"), messages[key].encode("utf-8")
        offsets.append((len(ids), len(key_bytes), len(strs), len(value_bytes)))
        ids += key_bytes + b"\0"
        strs += value_bytes + b"\0"
    keys_at = 7 * 4 + 16 * len(keys)
    values_at = keys_at + len(ids)
    table = []
    for id_at, id_length, str_at, str_length in offsets:
        table += [id_length, id_at + keys_at]
    for id_at, id_length, str_at, str_length in offsets:
        table += [str_length, str_at + values_at]
    path = localedir / lang / "LC_MESSAGES" / (APP_ID + ".mo")
    path.parent.mkdir(parents=True)
    path.write_bytes(
        struct.pack("Iiiiiii", 0x950412DE, 0, len(keys), 7 * 4, 7 * 4 + len(keys) * 8, 0, 0)
        + array("i", table).tobytes()
        + ids
        + strs
    )
    return path


@pytest.fixture(autouse=True)
def restore_gettext_state():
    """``setup()`` changes the process-wide text domain; the other tests get it back."""
    domain, localedir = gettext.textdomain(), gettext.bindtextdomain(APP_ID)
    yield
    gettext.textdomain(domain)
    gettext.bindtextdomain(APP_ID, localedir)


@pytest.fixture
def localedir(tmp_path, monkeypatch):
    """A directory with a Hungarian catalog, named by SLIDESHOW_LOCK_LOCALEDIR."""
    directory = tmp_path / "locale"
    write_mo(directory, "hu", HUNGARIAN)
    monkeypatch.setenv(i18n.LOCALEDIR_ENV, str(directory))
    return directory


def language(monkeypatch, **variables):
    """Exactly these language variables: the other three are unset."""
    for name in i18n.LANGUAGE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)


def test_every_test_starts_with_the_language_c(monkeypatch):
    """conftest.py pins it: the language of the machine that runs the suite does not matter."""
    assert os.environ["LANGUAGE"] == "C"


def test_the_catalog_of_the_language_is_loaded(localedir, monkeypatch):
    language(monkeypatch, LANGUAGE="hu")
    status = i18n.setup()
    assert _("Slideshow Lock") == "Diavetítés-zár"
    assert status.catalogs == (str(localedir / "hu" / "LC_MESSAGES" / (APP_ID + ".mo")),)
    assert status.localedir == str(localedir)


def test_a_string_the_catalog_does_not_have_stays_english(localedir, monkeypatch):
    language(monkeypatch, LANGUAGE="hu")
    i18n.setup()
    assert _("picture order") == "picture order"


def test_without_a_catalog_the_interface_is_english_and_nothing_fails(tmp_path, monkeypatch):
    language(monkeypatch, LANGUAGE="hu")
    monkeypatch.setenv(i18n.LOCALEDIR_ENV, str(tmp_path / "does-not-exist"))
    status = i18n.setup()
    assert _("Slideshow Lock") == "Slideshow Lock"
    assert status.catalogs == ()


def test_the_language_c_is_not_translated_even_when_a_catalog_exists(localedir, monkeypatch):
    language(monkeypatch, LANGUAGE="C", LANG="hu_HU.UTF-8")
    i18n.setup()
    assert _("Slideshow Lock") == "Slideshow Lock"


@pytest.mark.parametrize(
    "variables",
    [
        {"LANGUAGE": "hu"},
        {"LANGUAGE": "de:hu"},
        {"LC_ALL": "hu_HU.UTF-8"},
        {"LC_MESSAGES": "hu_HU.UTF-8"},
        {"LANG": "hu_HU.UTF-8"},
        {"LANG": "hu"},
    ],
)
def test_every_language_variable_python_reads_selects_the_catalog(
    localedir, monkeypatch, variables
):
    language(monkeypatch, **variables)
    i18n.setup()
    assert _("Slideshow Lock") == "Diavetítés-zár"


def test_the_first_variable_that_is_not_empty_wins(localedir, monkeypatch):
    language(monkeypatch, LANGUAGE="", LC_ALL="C", LANG="hu_HU.UTF-8")
    i18n.setup()
    assert _("Slideshow Lock") == "Slideshow Lock"
    language(monkeypatch, LANGUAGE="hu", LC_ALL="C")
    i18n.setup()
    assert _("Slideshow Lock") == "Diavetítés-zár"


def test_a_relative_localedir_is_made_absolute(localedir, monkeypatch):
    language(monkeypatch, LANGUAGE="hu")
    monkeypatch.chdir(localedir.parent)
    monkeypatch.setenv(i18n.LOCALEDIR_ENV, "locale")
    assert i18n.setup().localedir == str(localedir)


def test_without_the_variable_the_default_directory_of_python_gettext_is_used(monkeypatch):
    language(monkeypatch, LANGUAGE="hu")
    monkeypatch.delenv(i18n.LOCALEDIR_ENV, raising=False)
    assert i18n.setup().localedir == gettext.bindtextdomain(APP_ID)
    assert gettext.textdomain() == APP_ID


def test_the_status_line_names_the_language_variables_the_directory_and_the_catalog(
    localedir, monkeypatch, caplog
):
    language(monkeypatch, LANGUAGE="hu", LANG="hu_HU.UTF-8")
    status = i18n.setup()
    with caplog.at_level(logging.INFO):
        i18n.log_status(status)
    (record,) = caplog.records
    assert record.levelno == logging.INFO
    assert "\n" not in record.getMessage()
    assert record.getMessage() == (
        "[config] language LANGUAGE=hu LC_ALL=unset LC_MESSAGES=unset LANG=hu_HU.UTF-8, "
        "localedir %s, catalog %s"
        % (localedir, localedir / "hu" / "LC_MESSAGES" / (APP_ID + ".mo"))
    )


def test_the_status_line_says_so_when_there_is_no_catalog(tmp_path, monkeypatch, caplog):
    language(monkeypatch, LANGUAGE="hu")
    monkeypatch.setenv(i18n.LOCALEDIR_ENV, str(tmp_path))
    status = i18n.setup()
    with caplog.at_level(logging.INFO):
        i18n.log_status(status)
    assert "catalog none (the interface stays English)" in caplog.messages[0]


ENTRY_POINTS = [preview_app, service, preferences]


@pytest.mark.parametrize("module", ENTRY_POINTS, ids=lambda module: module.__name__)
def test_the_help_text_is_translated_so_the_language_is_chosen_before_the_command_line_is_read(
    module, localedir, monkeypatch, capsys
):
    language(monkeypatch, LANGUAGE="hu")
    with pytest.raises(SystemExit) as stopped:
        module.main(["--help"])
    assert stopped.value.code == 0
    assert "minden lépés naplózása" in capsys.readouterr().out


@pytest.mark.parametrize("module", ENTRY_POINTS, ids=lambda module: module.__name__)
def test_every_entry_point_logs_the_language_it_was_given(
    module, localedir, monkeypatch, caplog, capsys
):
    language(monkeypatch, LANGUAGE="hu")
    # No settings schema: main() ends with status 2 right after the logging is set up.
    monkeypatch.setattr(Gio.SettingsSchemaSource, "get_default", staticmethod(lambda: None))
    with caplog.at_level(logging.INFO):
        assert module.main([]) == 2
    lines = [m for m in caplog.messages if m.startswith("[config] language ")]
    assert len(lines) == 1
    assert "LANGUAGE=hu" in lines[0] and str(localedir) in lines[0] and ".mo" in lines[0]
    capsys.readouterr()
