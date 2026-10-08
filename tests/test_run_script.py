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
import time
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


def _run(args, env, script: Path = RUN_SH, cwd=None):
    return subprocess.run(
        [BASH, str(script), *args],
        env=env,
        cwd=cwd,
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
def no_adw_bin(tmp_path):
    """A PATH directory whose python3 is the real one, except that the Adw typelib cannot be loaded
    (it answers the ``typelib Adw 1`` probe of ``run.sh`` with a failure)."""
    bin_dir = tmp_path / "no-adw-bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "python3",
        '#!/bin/sh\nif [ "$1" = "-c" ] && [ "$3" = "Adw" ]; then exit 1; fi\n'
        f'exec "{sys.executable}" "$@"\n',
    )
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
        '  echo "ARGC=$#"\n'
        '  echo "LOCALEDIR=${SLIDESHOW_LOCK_LOCALEDIR-unset}"\n'
        '  echo "ARGC=$#"\n'
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


HUNGARIAN_PO = (
    'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
    'msgid "Slideshow Lock"\nmsgstr "Diavetítés-zár"\n'
)
BROKEN_PO = (
    'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
    '#, python-format\nmsgid "%d seconds"\nmsgstr "%s másodperc"\n'
)


def _checkout_with_catalog(tmp_path: Path, po_text: str) -> Path:
    """A checkout with the schema, the translation tool, and po/hu.po (listed in po/LINGUAS)."""
    script = _checkout_copy(tmp_path, with_schema=True)
    root = script.parent
    (root / "tools").mkdir()
    shutil.copy2(REPO / "tools" / "i18n.sh", root / "tools" / "i18n.sh")
    (root / "slideshow_lock").mkdir()
    shutil.copy2(REPO / "slideshow_lock" / "__init__.py", root / "slideshow_lock" / "__init__.py")
    (root / "po").mkdir()
    (root / "po" / "LINGUAS").write_text("hu\n")
    (root / "po" / "hu.po").write_text(po_text, encoding="utf-8")
    return script


def _need_msgfmt():
    if shutil.which("msgfmt") is None:
        pytest.skip("msgfmt is not on PATH (package gettext); the CI installs it before pytest")


def _path_without_msgfmt(tmp_path: Path, stub_bin: Path) -> str:
    """The stub python3 and the few programs run.sh uses before the program starts, no msgfmt."""
    bin_dir = tmp_path / "no-msgfmt-bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(stub_bin / "python3")
    for name in (
        "glib-compile-schemas",
        "cp",
        "mkdir",
        "rm",
        "mktemp",
        "chmod",
        "ln",
        "mv",
        "find",
    ):
        (bin_dir / name).symlink_to(shutil.which(name))
    return str(bin_dir)


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


def test_check_names_a_missing_libadwaita_which_the_settings_window_needs(tmp_path, no_adw_bin):
    """preferences.py requires the Adw 1 typelib; without the check, ``./run.sh settings`` passed
    ``check`` and then died with a raw ValueError."""
    result = _run(["check"], _env(tmp_path, path=f"{no_adw_bin}{os.pathsep}{os.environ['PATH']}"))
    assert result.returncode != 0
    assert "MISSING: libadwaita" in result.stderr
    assert "Adw-1 typelib" in result.stderr
    assert "1 required item(s) missing" in result.stderr


def test_check_passes_when_everything_is_installed(tmp_path):
    result = _run(["check"], _env(tmp_path))
    assert result.returncode == 0, result.stderr
    for item in (
        "gi (PyGObject)",
        "GTK 4",
        "libadwaita",
        "GdkPixbuf",
        "glib-compile-schemas",
        "Wayland session",
    ):
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
    assert "STUB -m slideshow_lock.settings_app" in result.stdout


def test_settings_passes_the_arguments_on_to_the_window_module(tmp_path, stub_bin):
    path = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    result = _run(["settings", "--debug", "/x y"], _env(tmp_path, path=path))
    assert result.returncode == 0, result.stderr
    assert "STUB -m slideshow_lock.settings_app --debug /x y" in result.stdout
    assert "ARGC=4" in result.stdout, "an argument was split or merged on the way"


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


APP_ID_MO = "io.github.trensoft.slideshowlock.mo"


def test_without_a_po_directory_the_locale_directory_is_not_set(tmp_path, stub_bin):
    script = _checkout_copy(tmp_path, with_schema=True)
    path = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    result = _run(["preview"], _env(tmp_path, path=path), script=script)
    assert result.returncode == 0, result.stderr
    assert "LOCALEDIR=unset" in result.stdout
    assert "msgfmt" not in result.stderr
    assert not (tmp_path / "cache" / "slideshow-lock" / "locale").exists()


@pytest.mark.parametrize("command", ["preview", "settings", "service"])
def test_the_catalogs_are_built_outside_the_checkout_and_the_program_is_pointed_at_them(
    tmp_path, stub_bin, command
):
    _need_msgfmt()
    script = _checkout_with_catalog(tmp_path, HUNGARIAN_PO)
    (script.parent / "slideshow_lock" / "preferences.py").write_text("")
    before = _snapshot(script.parent / "po"), _snapshot(script.parent / "data")
    path = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    result = _run([command], _env(tmp_path, path=path), script=script)
    assert result.returncode == 0, result.stderr
    locale = tmp_path / "cache" / "slideshow-lock" / "locale"
    assert f"LOCALEDIR={locale}" in result.stdout
    assert (locale / "hu" / "LC_MESSAGES" / APP_ID_MO).is_file()
    assert (_snapshot(script.parent / "po"), _snapshot(script.parent / "data")) == before


def _prepare(tmp_path, script, stub_bin, *, front=None):
    path = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    if front is not None:
        path = f"{front}{os.pathsep}{path}"
    return _run(["preview"], _env(tmp_path, path=path), script=script)


def test_the_locale_directory_is_a_link_to_a_directory_that_was_built_apart(tmp_path, stub_bin):
    """Two run.sh started together must not delete or rebuild what the other one is reading."""
    _need_msgfmt()
    script = _checkout_with_catalog(tmp_path, HUNGARIAN_PO)
    assert _prepare(tmp_path, script, stub_bin).returncode == 0
    locale = tmp_path / "cache" / "slideshow-lock" / "locale"
    assert locale.is_symlink()
    built = locale.resolve()
    assert built != locale and built.parent == locale.parent
    assert (built / "hu" / "LC_MESSAGES" / APP_ID_MO).is_file()
    assert (locale / "hu" / "LC_MESSAGES" / APP_ID_MO).is_file()


def test_the_catalogs_that_are_in_use_stay_in_place_while_the_new_ones_are_built(
    tmp_path, stub_bin
):
    _need_msgfmt()
    script = _checkout_with_catalog(tmp_path, HUNGARIAN_PO)
    assert _prepare(tmp_path, script, stub_bin).returncode == 0
    in_use = tmp_path / "cache" / "slideshow-lock" / "locale" / "hu" / "LC_MESSAGES" / APP_ID_MO
    probe = tmp_path / "probe-bin"
    probe.mkdir()
    seen = tmp_path / "seen"
    # Records, at the start of the build and at the swap, whether the catalogs in use are there.
    for name in ("msgfmt", "mv"):
        _executable(
            probe / name,
            '#!/bin/sh\nif [ -f "%s" ]; then state=present; else state=absent; fi\n'
            'echo "%s $state" >> "%s"\nexec "%s" "$@"\n' % (in_use, name, seen, shutil.which(name)),
        )
    result = _prepare(tmp_path, script, stub_bin, front=probe)
    assert result.returncode == 0, result.stderr
    assert seen.read_text().split("\n") == ["msgfmt present", "mv present", ""]
    assert in_use.is_file()


def test_the_directory_that_was_replaced_is_deleted_a_minute_later_not_at_once(tmp_path, stub_bin):
    _need_msgfmt()
    script = _checkout_with_catalog(tmp_path, HUNGARIAN_PO)
    locale = tmp_path / "cache" / "slideshow-lock" / "locale"
    assert _prepare(tmp_path, script, stub_bin).returncode == 0
    first = locale.resolve()
    assert _prepare(tmp_path, script, stub_bin).returncode == 0
    second = locale.resolve()
    assert second != first and first.is_dir()  # a run that is still starting may be reading it
    stale = time.time() - 120
    os.utime(first, (stale, stale))
    assert _prepare(tmp_path, script, stub_bin).returncode == 0
    assert not first.exists()
    assert second.is_dir() and locale.resolve() not in (first, second)


def test_a_plain_directory_left_by_an_older_run_sh_is_replaced_by_the_link(tmp_path, stub_bin):
    _need_msgfmt()
    script = _checkout_with_catalog(tmp_path, HUNGARIAN_PO)
    locale = tmp_path / "cache" / "slideshow-lock" / "locale"
    (locale / "xx").mkdir(parents=True)
    result = _prepare(tmp_path, script, stub_bin)
    assert result.returncode == 0, result.stderr
    assert locale.is_symlink() and not (locale / "xx").exists()
    assert (locale / "hu" / "LC_MESSAGES" / APP_ID_MO).is_file()


def test_a_catalog_that_is_gone_from_the_checkout_is_gone_from_the_built_directory(
    tmp_path, stub_bin
):
    _need_msgfmt()
    script = _checkout_with_catalog(tmp_path, HUNGARIAN_PO)
    path = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    assert _run(["preview"], _env(tmp_path, path=path), script=script).returncode == 0
    (script.parent / "po" / "LINGUAS").write_text("")
    assert _run(["preview"], _env(tmp_path, path=path), script=script).returncode == 0
    assert not list((tmp_path / "cache" / "slideshow-lock" / "locale").rglob("*.mo"))


def test_without_msgfmt_run_sh_warns_once_and_goes_on_in_english(tmp_path, stub_bin):
    script = _checkout_with_catalog(tmp_path, HUNGARIAN_PO)
    path = _path_without_msgfmt(tmp_path, stub_bin)
    result = _run(["preview"], _env(tmp_path, path=path), script=script)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("WARNING: msgfmt was not found") == 1
    assert "the interface stays English" in result.stderr
    locale = tmp_path / "cache" / "slideshow-lock" / "locale"
    assert f"LOCALEDIR={locale}" in result.stdout
    assert locale.is_dir() and not list(locale.rglob("*.mo"))


def test_check_mentions_a_missing_msgfmt_only_when_there_is_a_catalog(tmp_path, stub_bin):
    path = _path_without_msgfmt(tmp_path, stub_bin)
    with_catalog = _checkout_with_catalog(tmp_path, HUNGARIAN_PO)
    assert "msgfmt was not found" in _run(["check"], _env(tmp_path, path=path), with_catalog).stderr
    other = tmp_path / "other"
    other.mkdir()
    without = _checkout_copy(other, with_schema=True)
    assert "msgfmt" not in _run(["check"], _env(tmp_path, path=path), without).stderr


def test_a_catalog_that_does_not_compile_is_a_warning_and_the_interface_stays_english(
    tmp_path, stub_bin
):
    _need_msgfmt()
    script = _checkout_with_catalog(tmp_path, BROKEN_PO)
    path = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    result = _run(["preview"], _env(tmp_path, path=path), script=script)
    assert result.returncode == 0, result.stderr
    assert "the translations could not be built" in result.stderr
    locale = tmp_path / "cache" / "slideshow-lock" / "locale"
    assert f"LOCALEDIR={locale}" in result.stdout
    assert not list(locale.rglob("*.mo"))


def test_a_relative_cache_directory_that_starts_with_a_hyphen_is_a_path_not_an_option(
    tmp_path, stub_bin
):
    """XDG_CACHE_HOME=-x: mktemp, find and glib-compile-schemas must not read it as an option."""
    _need_msgfmt()
    script = _checkout_with_catalog(tmp_path, HUNGARIAN_PO)
    work = tmp_path / "work"
    work.mkdir()
    env = _env(tmp_path, path=f"{stub_bin}{os.pathsep}{os.environ['PATH']}")
    env["XDG_CACHE_HOME"] = "-x"
    result = _run(["preview"], env, script=script, cwd=work)
    assert result.returncode == 0, result.stderr
    assert "invalid option" not in result.stderr and "unknown predicate" not in result.stderr
    assert "LOCALEDIR=./-x/slideshow-lock/locale" in result.stdout
    assert (work / "-x" / "slideshow-lock" / "locale" / "hu" / "LC_MESSAGES" / APP_ID_MO).is_file()


def test_the_schema_is_compiled_under_a_relative_cache_directory_that_starts_with_a_hyphen(
    tmp_path, stub_bin
):
    script = _checkout_copy(tmp_path, with_schema=True)
    work = tmp_path / "work"
    work.mkdir()
    env = _env(tmp_path, path=f"{stub_bin}{os.pathsep}{os.environ['PATH']}")
    env["XDG_CACHE_HOME"] = "-x"
    result = _run(["preview"], env, script=script, cwd=work)
    assert result.returncode == 0, result.stderr
    assert "SCHEMA_DIR=./-x/slideshow-lock/schemas" in result.stdout
    assert (work / "-x" / "slideshow-lock" / "schemas" / "gschemas.compiled").is_file()
