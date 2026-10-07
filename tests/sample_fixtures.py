"""Helpers for the tests of the sample pictures: a package folder made of small fake pictures, and
the isolation that keeps a test that runs ``control.main()`` away from the real home and the real
``/usr/share``. Standard library only (``tests/test_sample_pictures.py`` runs without GTK)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from slideshow_lock import sample_pictures
from tests.jpeg_fixtures import fake_jpeg

#: What the fake package holds, and what each picture is made of (distinct sizes and bytes).
PICTURES = ("fr01.jpg", "fr02.jpg", "fr03.jpg")

#: An hour old: the picture reader holds back a file younger than two seconds, and a test that
#: wants to see whether the copy keeps the time of the source needs a source that is not fresh.
SOURCE_AGE_SECONDS = 3600


def make_source(root: Path, names: Iterable[str] = PICTURES, credits: bool = True) -> str:
    """``root/share/slideshow-lock/pictures`` with one fake picture per name (and the credits file);
    the path. Every file has the same old time, the contents differ by name."""
    folder = root / "share" / sample_pictures.APP_DIR / sample_pictures.DATA_SUBDIR
    folder.mkdir(parents=True, exist_ok=True)
    old = os.stat(folder).st_mtime - SOURCE_AGE_SECONDS
    for index, name in enumerate(names):
        path = folder / name
        path.write_bytes(fake_jpeg(500 + 100 * index))
        os.utime(path, (old, old))
    if credits:
        path = folder / sample_pictures.CREDITS_NAME
        path.write_text("Fraktalkepek: a test\n")
        os.utime(path, (old, old))
    return str(folder)


def isolate_sample_pictures(monkeypatch, root: Path) -> Path:
    """HOME and XDG_STATE_HOME under *root*, and the package folders (``DATA_DIRS``) an empty
    folder: a test that runs the real ``control.main`` finds no pictures to copy and has no
    way to write into the real home. Returns the folder that stands for ``/usr/share``."""
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    share = root / "share"
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    monkeypatch.setattr(sample_pictures, "DATA_DIRS", (str(share),))
    return share
