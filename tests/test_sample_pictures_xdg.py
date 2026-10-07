"""The sample pictures go into the default picture folder, in every language of the system.

GLib reads ``user-dirs.dirs`` once per process and keeps it, so each case starts a fresh interpreter
with ``HOME`` and ``XDG_CONFIG_HOME`` already set (see ``test_picture_folder_xdg.py``) and runs the
real ``control.ensure_sample_pictures`` and the real copy into a package folder made for the test.
Three systems: a Hungarian one (``$HOME/Képek``, a path with an accent), one with no pictures
directory configured, and one whose pictures directory is the home directory itself (the XDG way to
say "off"). The pictures always land in ``default_picture_folder()/trensoft``.

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

from tests.sample_fixtures import PICTURES, make_source

pytestmark = pytest.mark.spawns_processes

REPO = Path(__file__).resolve().parent.parent

_PROBE = """
import json, os
from slideshow_lock import control, sample_pictures
from slideshow_lock.settings import Settings, default_picture_folder

sample_pictures.DATA_DIRS = (os.environ["SAMPLE_SHARE"],)
thread = control.ensure_sample_pictures()
thread.join(30)
target = os.path.join(default_picture_folder(), sample_pictures.SUBDIR)
print(json.dumps({
    "default": default_picture_folder(),
    "copied": sorted(os.listdir(target)) if os.path.isdir(target) else None,
    "user_value": Settings()._settings.get_user_value("picture-folder") is not None,
}, ensure_ascii=False))
"""


def _run(tmp_path: Path, user_dirs: "str | None"):
    home = tmp_path / "home"
    config = tmp_path / "config"
    home.mkdir()
    config.mkdir()
    if user_dirs is not None:
        (config / "user-dirs.dirs").write_text(user_dirs, encoding="utf-8")
    make_source(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(config)
    env["PYTHONIOENCODING"] = "utf-8"
    env["SAMPLE_SHARE"] = str(tmp_path / "share")
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
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


@pytest.mark.parametrize(
    ("user_dirs", "folder"),
    [
        ('XDG_PICTURES_DIR="$HOME/Képek"\n', "Képek"),  # a Hungarian system
        (None, "Pictures"),  # nothing configured
        ('XDG_PICTURES_DIR="$HOME"\n', "Pictures"),  # the home directory itself means "off"
    ],
    ids=["hungarian", "not-configured", "home-means-off"],
)
def test_the_pictures_land_in_the_default_folder_of_the_system(tmp_path, user_dirs, folder):
    home, got = _run(tmp_path, user_dirs)
    assert got["default"] == f"{home}/{folder}"
    assert got["copied"] == sorted(PICTURES + ("CREDITS.txt",))
    assert got["user_value"] is False  # the picture-folder key is not written
    assert not (Path(home) / "trensoft").exists()  # never in the home directory itself
