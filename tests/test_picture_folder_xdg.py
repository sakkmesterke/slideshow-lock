"""The default picture folder follows the user's XDG pictures directory, in every language.

GLib reads ``~/.config/user-dirs.dirs`` once per process and keeps the result (measured: a file
changed while the process runs is not seen until ``GLib.reload_user_special_dirs_cache()``, and
``XDG_CONFIG_HOME`` itself is cached too). A test that wants a different file therefore cannot
change it in the pytest process: each case starts a fresh interpreter with ``HOME`` and
``XDG_CONFIG_HOME`` already set. That is also the honest test, because it is what a real start of
the program does.

The cases: a Hungarian system (``$HOME/Képek``, a non-ASCII path), no configured pictures
directory, a ``user-dirs.dirs`` without the pictures line, a pictures directory that is the home
directory itself (the XDG way to say "off"), and an unusable (relative) value. Every start answers
for the three readers of the folder: ``default_picture_folder()``, ``Settings.get_picture_folder()``
and the preview's ``SessionSettings`` (with and without ``--folder``), plus the settings window's
model.

Each test starts a program on purpose, so the module opts out of the tripwire; every test is listed
in ``OPT_OUTS`` of ``test_tripwire.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spawns_processes

REPO = Path(__file__).resolve().parent.parent

_PROBE = """
import json
from slideshow_lock.preferences_model import PreferencesModel
from slideshow_lock.preview_app import SessionSettings
from slideshow_lock.settings import Settings, default_picture_folder

settings = Settings()
view = PreferencesModel(settings).folder_view()
override = SessionSettings(settings, {"picture-folder": "/chosen"})
print(json.dumps({
    "default": default_picture_folder(),
    "stored": settings.get_picture_folder(),
    "preview": SessionSettings(settings, {}).get_picture_folder(),
    "preview_with_folder": override.get_picture_folder(),
    "window_hint": view.default,
    "window_field": view.text,
    "window_note": view.note,
}, ensure_ascii=False))
"""


def _start(tmp_path: Path, user_dirs: "str | None", stored: "str | None" = None):
    """Start a fresh interpreter in a home of its own, with this ``user-dirs.dirs`` (or none)."""
    home = tmp_path / "home"
    config = tmp_path / "config"
    home.mkdir(exist_ok=True)
    config.mkdir(exist_ok=True)
    if user_dirs is not None:
        (config / "user-dirs.dirs").write_text(user_dirs, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(config)
    env["PYTHONIOENCODING"] = "utf-8"
    code = (
        _PROBE
        if stored is None
        else _PROBE.replace(
            "settings = Settings()\n",
            f"settings = Settings()\nsettings.set_picture_folder({stored!r})\n",
        )
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return str(home), json.loads(result.stdout)


def test_a_hungarian_system_uses_its_kepek_folder_everywhere(tmp_path):
    home, got = _start(tmp_path, 'XDG_PICTURES_DIR="$HOME/Képek"\n')
    expected = f"{home}/Képek/slideshow-lock"
    assert got["default"] == expected
    assert got["stored"] == expected
    assert got["preview"] == expected
    assert got["window_hint"] == expected
    assert got["window_field"] == ""  # the default is in use: the field is empty, the hint shows it
    assert expected in got["window_note"]
    assert "/Pictures" not in json.dumps(got, ensure_ascii=False)


def test_a_folder_given_to_the_preview_wins_over_the_system_folder(tmp_path):
    _home, got = _start(tmp_path, 'XDG_PICTURES_DIR="$HOME/Képek"\n')
    assert got["preview_with_folder"] == "/chosen"


def test_a_stored_folder_wins_over_the_system_folder(tmp_path):
    _home, got = _start(tmp_path, 'XDG_PICTURES_DIR="$HOME/Képek"\n', stored="/stored/pictures")
    assert got["stored"] == "/stored/pictures"
    assert got["preview"] == "/stored/pictures"


def test_another_language_folder_name_is_followed_too(tmp_path):
    home, got = _start(tmp_path, 'XDG_PICTURES_DIR="$HOME/Obrázky"\n')
    assert got["stored"] == f"{home}/Obrázky/slideshow-lock"


def test_without_a_user_dirs_file_it_falls_back_to_home_pictures(tmp_path):
    home, got = _start(tmp_path, None)
    expected = f"{home}/Pictures/slideshow-lock"
    assert got["default"] == expected
    assert got["stored"] == expected
    assert got["preview"] == expected
    assert got["window_hint"] == expected


def test_a_user_dirs_file_without_the_pictures_line_falls_back_too(tmp_path):
    home, got = _start(tmp_path, 'XDG_MUSIC_DIR="$HOME/Zene"\n')
    assert got["stored"] == f"{home}/Pictures/slideshow-lock"


@pytest.mark.parametrize("line", ['XDG_PICTURES_DIR="$HOME"\n', 'XDG_PICTURES_DIR="$HOME/"\n'])
def test_a_pictures_dir_that_is_the_home_directory_means_off_and_falls_back(tmp_path, line):
    # XDG writes the home directory for "no pictures directory". GLib returns it unchanged
    # (measured, GLib 2.74), so the slideshow would look in "$HOME/slideshow-lock" and the "off"
    # would be read as a real choice. It is treated like an unset one.
    home, got = _start(tmp_path, line)
    assert got["stored"] == f"{home}/Pictures/slideshow-lock"


def test_a_relative_pictures_dir_is_not_used(tmp_path):
    home, got = _start(tmp_path, 'XDG_PICTURES_DIR="Képek"\n')
    assert got["stored"] == f"{home}/Pictures/slideshow-lock"
