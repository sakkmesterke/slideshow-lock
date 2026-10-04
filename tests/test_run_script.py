"""Tests for ``run.sh``, the launcher a person uses to try the project from a checkout.

The script is run for real, with ``bash``. Its dependencies are replaced where the test needs a
machine without them: a ``python3`` that cannot import ``gi`` (the real interpreter, started
isolated and without site-packages), a ``PATH`` without ``glib-compile-schemas``, and a ``python3``
that only records how ``-m`` was called. Nothing here opens a window: the one test that runs the
real ``check`` needs the GI stack that the CI installs (the verify step names a missing package),
so on a machine without it that test fails, it does not skip.

Every test starts a program on purpose, so the module opts out of the tripwire as a whole; each
test is listed in ``OPT_OUTS`` of ``test_tripwire.py``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spawns_processes

REPO = Path(__file__).resolve().parent.parent
RUN_SH = REPO / "run.sh"
BASH = shutil.which("bash") or "/bin/bash"
DUMMY_WAYLAND = "wayland-run-script-test"


def _executable(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _env(tmp_path: Path, *, path=None, wayland=DUMMY_WAYLAND) -> dict:
    env = dict(os.environ)
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("GSETTINGS_SCHEMA_DIR", None)
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    if path is not None:
        env["PATH"] = path
    if wayland:
        env["WAYLAND_DISPLAY"] = wayland
    return env


def _run(args, env, script: Path = RUN_SH):
    return subprocess.run(
        [BASH, str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def no_gi_bin(tmp_path):
    """A PATH directory whose only program is a python3 that cannot import gi."""
    bin_dir = tmp_path / "no-gi-bin"
    bin_dir.mkdir()
    _executable(bin_dir / "python3", f'#!/bin/sh\nexec "{sys.executable}" -I -S "$@"\n')
    return bin_dir


@pytest.fixture
def stub_bin(tmp_path):
    """A python3 that answers ``-m`` by printing what it got and passes everything else on."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "python3",
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ]; then\n'
        '  echo "STUB $*"\n'
        '  echo "SCHEMA_DIR=$GSETTINGS_SCHEMA_DIR"\n'
        '  echo "PYTHONPATH=$PYTHONPATH"\n'
        "  exit 0\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
    )
    return bin_dir


def _checkout_copy(tmp_path: Path, *, with_schema: bool) -> Path:
    """A checkout that has run.sh and a schema, but no settings window."""
    root = tmp_path / "partial-checkout"
    root.mkdir()
    shutil.copy2(RUN_SH, root / "run.sh")
    if with_schema:
        shutil.copytree(REPO / "data", root / "data")
    return root / "run.sh"


def _snapshot(directory: Path):
    return sorted((p.name, p.stat().st_mtime_ns) for p in directory.iterdir())


def test_run_sh_is_executable_in_git():
    mode = subprocess.run(
        ["git", "ls-files", "--stage", "run.sh"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    assert mode == "100755"


def test_without_arguments_it_prints_the_usage_and_fails(tmp_path):
    result = _run([], _env(tmp_path))
    assert result.returncode == 2
    assert "usage: ./run.sh check" in result.stderr
    assert "preview" in result.stderr and "settings" in result.stderr and "service" in result.stderr


def test_an_unknown_command_prints_the_usage_and_fails(tmp_path):
    result = _run(["frobnicate"], _env(tmp_path))
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_check_names_every_missing_item_and_marks_the_package_names_unverified(tmp_path, no_gi_bin):
    result = _run(["check"], _env(tmp_path, path=str(no_gi_bin), wayland=None))
    assert result.returncode != 0
    err = result.stderr
    assert "MISSING: gi (PyGObject)" in err
    assert "python3-gobject" in err
    assert "MISSING: glib-compile-schemas" in err
    assert "glib2-devel" in err
    assert "MISSING: Wayland session" in err
    assert "WAYLAND_DISPLAY is not set" in err
    assert "3 required item(s) missing" in err
    assert "not verified on RHEL 10.2" in err


def test_check_without_python3_says_so(tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    result = _run(["check"], _env(tmp_path, path=str(empty)))
    assert result.returncode != 0
    assert "MISSING: python3" in result.stderr
    assert "Traceback" not in result.stderr


def test_check_fails_without_a_wayland_session(tmp_path):
    result = _run(["check"], _env(tmp_path, wayland=None))
    assert result.returncode != 0
    assert "MISSING: Wayland session" in result.stderr
    assert "1 required item(s) missing" in result.stderr


def test_check_passes_when_everything_is_installed(tmp_path):
    result = _run(["check"], _env(tmp_path))
    assert result.returncode == 0, result.stderr
    for item in ("gi (PyGObject)", "GTK 4", "GdkPixbuf", "glib-compile-schemas", "Wayland session"):
        assert f"ok:      {item}" in result.stdout
    assert "MISSING" not in result.stderr


def test_preview_compiles_the_schema_outside_the_checkout_and_passes_the_arguments(
    tmp_path, stub_bin
):
    before = (_snapshot(REPO), _snapshot(REPO / "data"))
    path = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    result = _run(["preview", "--folder", "/x y", "--interval", "3"], _env(tmp_path, path=path))
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "STUB -m slideshow_lock.preview_app --folder /x y --interval 3" in out
    schema_dir = tmp_path / "cache" / "slideshow-lock" / "schemas"
    assert f"SCHEMA_DIR={schema_dir}" in out
    assert (schema_dir / "gschemas.compiled").is_file()
    assert f"PYTHONPATH={REPO}" in out
    assert (_snapshot(REPO), _snapshot(REPO / "data")) == before, "run.sh touched the checkout"


def test_preview_without_the_dependencies_stops_with_the_report_and_no_traceback(
    tmp_path, no_gi_bin
):
    result = _run(["preview"], _env(tmp_path, path=str(no_gi_bin)))
    assert result.returncode == 1
    assert "MISSING: gi (PyGObject)" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "cache").exists()


def test_preview_in_a_checkout_without_the_schema_says_so(tmp_path, stub_bin):
    script = _checkout_copy(tmp_path, with_schema=False)
    path = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    result = _run(["preview"], _env(tmp_path, path=path), script=script)
    assert result.returncode == 1
    assert "no data/*.gschema.xml" in result.stderr
    assert "STUB" not in result.stdout


def test_settings_without_the_window_module_says_so_and_is_no_traceback(tmp_path, stub_bin):
    script = _checkout_copy(tmp_path, with_schema=True)
    path = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    result = _run(["settings"], _env(tmp_path, path=path), script=script)
    assert result.returncode == 1
    assert "not available yet" in result.stderr
    assert "Traceback" not in result.stderr
    assert "STUB" not in result.stdout


def test_settings_starts_the_window_module(tmp_path, stub_bin):
    path = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    result = _run(["settings"], _env(tmp_path, path=path))
    assert result.returncode == 0, result.stderr
    assert "STUB -m slideshow_lock.preferences" in result.stdout


def test_service_compiles_the_schema_outside_the_checkout_and_passes_the_arguments(
    tmp_path, stub_bin
):
    before = (_snapshot(REPO), _snapshot(REPO / "data"))
    path = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    result = _run(
        ["service", "--idle-timeout", "20", "--grace", "3", "--folder", "/x y"],
        _env(tmp_path, path=path),
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "STUB -m slideshow_lock.service --idle-timeout 20 --grace 3 --folder /x y" in out
    schema_dir = tmp_path / "cache" / "slideshow-lock" / "schemas"
    assert f"SCHEMA_DIR={schema_dir}" in out
    assert (schema_dir / "gschemas.compiled").is_file()
    assert f"PYTHONPATH={REPO}" in out
    assert (_snapshot(REPO), _snapshot(REPO / "data")) == before, "run.sh touched the checkout"


def test_service_without_the_dependencies_stops_with_the_report_and_no_traceback(
    tmp_path, no_gi_bin
):
    result = _run(["service"], _env(tmp_path, path=str(no_gi_bin)))
    assert result.returncode == 1
    assert "MISSING: gi (PyGObject)" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "cache").exists()
