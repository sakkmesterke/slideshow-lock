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

# No translation in any test: the language of the developer's session (LANGUAGE, LC_ALL, LANG) must
# not decide which text a test sees. A test of the translation sets its own language. Python's
# gettext reads LANGUAGE first and ``C`` stops the search, so this wins over the other three.
os.environ["LANGUAGE"] = "C"

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

# Import must happen after the env vars above are set (same ordering
# constraint as the schema-source caching note in the module docstring):
# constructing a `Settings()` is what triggers GLib's first (and only)
# resolution of the default schema source / backend.
from slideshow_lock.settings import (  # noqa: E402
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    KEY_ORDER,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_PICTURE_FOLDER,
    KEY_SCALING,
    KEY_SHOW_SCREENSHOTS,
    KEY_SLIDE_INTERVAL_SECONDS,
    KEY_TRANSITION_DURATION,
    KEY_TRANSITION_ORDER,
    KEY_TRANSITIONS,
    Settings,
)

_ALL_SETTINGS_KEYS = [
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    KEY_PICTURE_FOLDER,
    KEY_SLIDE_INTERVAL_SECONDS,
    KEY_ORDER,
    KEY_SCALING,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_SHOW_SCREENSHOTS,
    KEY_TRANSITIONS,
    KEY_TRANSITION_ORDER,
    KEY_TRANSITION_DURATION,
]


@pytest.fixture(autouse=True)
def _reset_gsettings_between_tests():
    """Reset every schema key to its default after each test.

    `GSETTINGS_BACKEND=memory` is a single, process-wide store: every
    `Gio.Settings` instance constructed anywhere in the test session
    (regardless of which test created it) reads and writes the *same*
    backing store. Without this fixture, a value written in one test
    (e.g. idle-timeout-seconds set to 300 by the roundtrip test) leaks
    into every later test that assumes it is starting from the schema
    defaults.
    """
    yield
    settings = Settings()
    for key in _ALL_SETTINGS_KEYS:
        settings._settings.reset(key)


#: Python-level ways to start a program or to reach a bus. The slideshow preview never locks
#: (D11): no test, and so no code a test runs, has any reason to call one of them. A call
#: raises, so the line that makes it is the failure. Complements the scan of the sources
#: (``test_preview.py``): this catches a call the names of which the scan cannot see (built
#: from pieces, looked up at runtime) but only where a test runs it. A call that sits in a
#: GTK event handler (the window's key handler, say) is not run by pytest at all, and the
#: Wayland smoke tool has no tripwire: nobody sees that one (only the scan of the sources, by
#: name). A test that really starts a program opts out with
#: ``@pytest.mark.spawns_processes``.
_OS_PROCESS_CALLS = (
    "system",
    "popen",
    "fork",
    "forkpty",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "posix_spawn",
    "posix_spawnp",
    "startfile",
)


#: The same for the Gio calls that start a program through a class and not a module function.
#: ``Gio.SubprocessLauncher.spawn`` is not in the typelib (a varargs call), so a name that is
#: missing is skipped, as with ``os`` above.
_GIO_PROCESS_CALLS = (
    ("Subprocess", ("new", "newv")),
    ("SubprocessLauncher", ("spawn", "spawnv")),
    (
        "AppInfo",
        ("create_from_commandline", "launch_default_for_uri", "launch_default_for_uri_async"),
    ),
)


class ForbiddenCall(AssertionError):
    pass


def _forbidden(what):
    def call(*args, **kwargs):
        raise ForbiddenCall(f"{what} was called: a preview must not start programs or use a bus")

    return call


@pytest.fixture(autouse=True)
def _no_process_or_bus_calls(request, monkeypatch):
    if request.node.get_closest_marker("spawns_processes"):
        yield
        return
    import gi

    gi.require_version("GLib", "2.0")
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    for name in _OS_PROCESS_CALLS:
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _forbidden(f"os.{name}"))
    monkeypatch.setattr(subprocess, "Popen", _forbidden("subprocess.Popen"))
    for module, prefix in ((GLib, "spawn_"), (Gio, "bus_")):
        for name in dir(module):
            if name.startswith(prefix):
                monkeypatch.setattr(module, name, _forbidden(f"{module.__name__}.{name}"))
    for class_name, names in _GIO_PROCESS_CALLS:
        cls = getattr(Gio, class_name)
        for name in names:
            if hasattr(cls, name):
                monkeypatch.setattr(cls, name, _forbidden(f"Gio.{class_name}.{name}"))
    yield
