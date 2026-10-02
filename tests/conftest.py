"""Shared fixtures for the CORE-3 settings tests.

These tests exercise a real `Gio.Settings` object against the project's own
gschema, compiled into a throwaway directory for the duration of the test
session. They require PyGObject and the `glib-compile-schemas` tool, which
are system packages, not something `pip install` alone can provide -- if
either is missing, the whole settings test module is skipped with a clear
reason instead of failing the collection step.

`GSETTINGS_BACKEND=memory` makes `Gio.Settings` use an in-process, ephemeral
backend instead of the real dconf database, so these tests never touch (or
depend on) the machine's actual settings.

The schema dir / backend env vars are set at **module import time** (i.e.
once per test session, before any test or fixture runs), not inside a
per-test fixture: GLib caches the default `GSettingsSchemaSource` the first
time any `Gio.Settings` is constructed in the process, so setting
`GSETTINGS_SCHEMA_DIR` again later in the same process would silently have
no effect.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

pytest.importorskip("gi", reason="PyGObject (gi) is not installed in this environment")

_GSCHEMA_COMPILER = shutil.which("glib-compile-schemas")
if _GSCHEMA_COMPILER is None:
    pytest.skip(
        "glib-compile-schemas is not on PATH; install libglib2.0-bin (Debian/Ubuntu) or "
        "glib2 (Fedora/RHEL) to run the CORE-3 settings tests",
        allow_module_level=True,
    )

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GSCHEMA_SOURCE_DIR = os.path.join(_REPO_ROOT, "data")

# Kept alive for the whole process so the directory is not cleaned up while
# GLib still has it cached as the schema source.
_compiled_schema_tmpdir = tempfile.TemporaryDirectory(prefix="slideshow-lock-gschema-")
_compiled_schema_dir = os.path.join(_compiled_schema_tmpdir.name, "glib-2.0", "schemas")
os.makedirs(_compiled_schema_dir)

for _name in os.listdir(_GSCHEMA_SOURCE_DIR):
    if _name.endswith(".gschema.xml"):
        shutil.copy(os.path.join(_GSCHEMA_SOURCE_DIR, _name), _compiled_schema_dir)

_result = subprocess.run(
    [_GSCHEMA_COMPILER, _compiled_schema_dir],
    capture_output=True,
    text=True,
)
if _result.returncode != 0:
    pytest.exit(
        f"glib-compile-schemas failed:\nstdout={_result.stdout}\nstderr={_result.stderr}",
        returncode=1,
    )

os.environ["GSETTINGS_SCHEMA_DIR"] = _compiled_schema_dir
os.environ["GSETTINGS_BACKEND"] = "memory"
