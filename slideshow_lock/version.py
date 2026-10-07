"""The version of this program, read from where it is already written down.

``pyproject.toml`` holds the one version number (the Fedora spec and the tag follow it, see
``docs/RELEASING.md``); this module makes no copy of it. Two places are asked, in this order:

* ``pyproject.toml`` next to the ``slideshow_lock`` package, which exists only in a source tree
  (a checkout run with ``run.sh``): there it is what the code beside it says, even when an older
  package is installed too;
* the installed package's metadata (``importlib.metadata``), which is where an RPM install
  has it, since the package is built from that same ``pyproject.toml``.

With neither (the package copied somewhere without its metadata) the answer is ``"dev"``: no
error, and no number that could be wrong.
"""

from __future__ import annotations

import os
import re
from typing import Optional

#: The distribution name in ``pyproject.toml`` (``[project] name``).
DISTRIBUTION = "slideshow-lock"

#: What stands in for a version that cannot be found.
UNKNOWN = "dev"

_VERSION_LINE = re.compile(r'^version\s*=\s*"([^"\n]+)"\s*$')


def _from_source_tree(package_dir: str) -> Optional[str]:
    """``version`` of the ``[project]`` table of the ``pyproject.toml`` one folder above
    *package_dir*, None if there is none, or it names another project, or has no version."""
    path = os.path.join(os.path.dirname(package_dir), "pyproject.toml")
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    table = ""
    name = None
    found = None
    for line in lines:
        text = line.strip()
        if text.startswith("["):
            table = text
            continue
        if table != "[project]":
            continue
        if text.startswith("name") and name is None:
            match = re.match(r'^name\s*=\s*"([^"\n]+)"\s*$', text)
            name = match.group(1) if match else None
        match = _VERSION_LINE.match(text)
        if match and found is None:
            found = match.group(1)
    return found if name == DISTRIBUTION else None


def _from_metadata() -> Optional[str]:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # Python before 3.8: not a version this program supports
        return None
    try:
        return version(DISTRIBUTION) or None
    except PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - a broken metadata folder must not stop the settings window
        return None


def program_version(package_dir: Optional[str] = None) -> str:
    """The version of the program, or ``UNKNOWN``. *package_dir* is the folder of the
    ``slideshow_lock`` package (the default: the one this module is in)."""
    if package_dir is None:
        package_dir = os.path.dirname(os.path.abspath(__file__))
    return _from_source_tree(package_dir) or _from_metadata() or UNKNOWN
