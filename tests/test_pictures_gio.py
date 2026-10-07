"""The pictures of ``data/pictures`` against the real picture code: the probe the service uses, the
file reader of the scaler and the real decoder (GdkPixbuf).

``tests/test_pictures_clean.py`` proves the bytes and the folder with the standard library alone.
What only the real loader can say is said here: every picture is accepted by ``probe_loadable``, is
read whole by ``read_image_file`` (a JPEG that ``jpeg_is_complete`` takes), is announced by the
loader within the pixel limit, and is decoded to the size its header names. No skip: a missing
loader is a red test (CI checks for the typelib before pytest runs).
"""

from __future__ import annotations

from pathlib import Path

import gi
import pytest

from slideshow_lock.scaling import (
    MAX_PIXELS,
    ImageScaler,
    ImageSkipped,
    probe_loadable,
    read_image_file,
)

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

PICTURES = Path(__file__).resolve().parent.parent / "data" / "pictures"
FILES = sorted(PICTURES.glob("*.jpg"))


def test_there_are_pictures_to_test():
    assert FILES


@pytest.mark.parametrize("picture", FILES, ids=lambda p: p.name)
def test_the_real_loader_takes_the_picture_and_the_scaler_does_not_skip_it(picture):
    probe_loadable(str(picture))  # raises ValueError without a loader for the format
    data = read_image_file(str(picture), settle_seconds=0)  # raises ImageSkipped if it is refused
    info, width, height = GdkPixbuf.Pixbuf.get_file_info(str(picture))
    assert info is not None and info.get_name() == "jpeg"
    assert width * height <= MAX_PIXELS
    try:
        pixbuf = ImageScaler._decode(data)
    except ImageSkipped as exc:  # the name of the picture in the message, not a bare failure
        pytest.fail(f"{picture.name}: {exc}")
    assert (pixbuf.get_width(), pixbuf.get_height()) == (width, height)
