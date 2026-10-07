"""Tests for the files the package installs next to the program: the launcher and the systemd unit.

``packaging/slideshow-lock`` is run for real, with ``sh``. Its interpreter, ``/usr/bin/python3``, is
replaced in a copy by a program that only records how it was called: the launcher's job is to pick
the module and pass the arguments on, and the real programs need a display and a bus. A test pins
the line that is replaced, so the copy cannot drift from the file that is installed.

The unit is read as text. ``systemd-analyze verify`` is not run here (it needs systemd, which the CI
image has no use for); the unit and the launcher are checked against each other instead.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "packaging" / "slideshow-lock"
SHORT_LAUNCHER = REPO / "packaging" / "slideshowlock"
SPEC = REPO / "packaging" / "fedora" / "slideshow-lock.spec"
UNIT = REPO / "data" / "slideshow-lock.service"
SH = shutil.which("sh") or "/bin/sh"
EXEC_LINE = 'exec /usr/bin/python3 -P -m "$module" "$@"'
SHORT_EXEC_LINE = 'exec /usr/bin/slideshow-lock control "$@"'

MODULES = {
    "service": "slideshow_lock.service",
    "settings": "slideshow_lock.settings_app",
    "control": "slideshow_lock.control",
    "preview": "slideshow_lock.preview_app",
}


def _unit_section(name: str) -> dict:
    """The directives of one section of the unit: name -> list of values (one per line)."""
    found: dict = {}
    section = None
    for line in UNIT.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line.strip("[]")
        elif section == name:
            key, _, value = line.partition("=")
            found.setdefault(key, []).append(value)
    return found


@pytest.fixture
def short_launcher(tmp_path):
    """A copy of the short command whose slideshow-lock prints how it was called."""
    text = SHORT_LAUNCHER.read_text()
    assert text.count(SHORT_EXEC_LINE) == 1, "the line the test replaces has changed"
    recorder = tmp_path / "slideshow-lock"
    recorder.write_text('#!/bin/sh\necho "ARGC=$#"\nfor a in "$@"; do echo "ARG=$a"; done\n')
    recorder.chmod(recorder.stat().st_mode | stat.S_IXUSR)
    copy = tmp_path / "slideshowlock"
    copy.write_text(text.replace("/usr/bin/slideshow-lock", str(recorder)))
    return copy


@pytest.fixture
def launcher(tmp_path):
    """A copy of the launcher whose interpreter prints how it was called."""
    text = LAUNCHER.read_text()
    assert text.count(EXEC_LINE) == 1, "the line the test replaces has changed"
    recorder = tmp_path / "python3"
    recorder.write_text('#!/bin/sh\necho "ARGC=$#"\nfor a in "$@"; do echo "ARG=$a"; done\n')
    recorder.chmod(recorder.stat().st_mode | stat.S_IXUSR)
    copy = tmp_path / "slideshow-lock"
    copy.write_text(text.replace("/usr/bin/python3", str(recorder)))
    return copy


def _run(script: Path, *args: str):
    return subprocess.run(
        [SH, str(script), *args], capture_output=True, text=True, timeout=30, check=False
    )


@pytest.mark.spawns_processes
def test_the_launcher_is_executable_in_git():
    mode = subprocess.run(
        ["git", "ls-files", "--stage", "packaging/slideshow-lock"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    assert mode == "100755"


@pytest.mark.spawns_processes
def test_the_short_command_is_executable_in_git():
    mode = subprocess.run(
        ["git", "ls-files", "--stage", "packaging/slideshowlock"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    assert mode == "100755"


@pytest.mark.spawns_processes
@pytest.mark.parametrize("command", sorted(MODULES))
def test_each_command_starts_its_module_isolated_from_the_current_directory(launcher, command):
    result = _run(launcher, command)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["ARGC=3", "ARG=-P", "ARG=-m", f"ARG={MODULES[command]}"]


@pytest.mark.spawns_processes
def test_the_arguments_go_on_unchanged_and_in_order(launcher):
    result = _run(launcher, "service", "--folder", "/x y", "--idle-timeout", "20", "--debug")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[4:] == [
        "ARG=--folder",
        "ARG=/x y",
        "ARG=--idle-timeout",
        "ARG=20",
        "ARG=--debug",
    ]


@pytest.mark.spawns_processes
def test_help_after_a_command_is_the_programs_own(launcher):
    result = _run(launcher, "settings", "--help")
    assert result.stdout.splitlines()[-1] == "ARG=--help"


@pytest.mark.spawns_processes
@pytest.mark.parametrize("args", [(), ("frobnicate",), ("--service",), ("",)])
def test_no_command_or_an_unknown_one_prints_the_usage_and_fails(launcher, args):
    result = _run(launcher, *args)
    assert result.returncode == 2
    assert result.stdout == ""
    for command in MODULES:
        assert f"slideshow-lock {command}" in result.stderr


@pytest.mark.spawns_processes
@pytest.mark.parametrize("flag", ["--help", "-h", "help"])
def test_help_prints_the_usage_and_succeeds_without_starting_anything(launcher, flag):
    result = _run(launcher, flag)
    assert result.returncode == 0
    assert result.stderr == ""
    assert "ARGC=" not in result.stdout
    for command in MODULES:
        assert f"slideshow-lock {command}" in result.stdout


def test_every_module_the_launcher_names_exists_in_the_package():
    text = LAUNCHER.read_text()
    for module in MODULES.values():
        assert f"={module} ;;" in text
        assert (REPO / module.replace(".", "/")).with_suffix(".py").is_file()


@pytest.mark.spawns_processes
def test_the_unit_runs_the_launcher_with_the_service_command(launcher):
    [exec_start] = _unit_section("Service")["ExecStart"]
    program, command, *rest = exec_start.split()
    assert (program, command, rest) == ("/usr/bin/slideshow-lock", "service", [])
    result = _run(launcher, command)
    assert result.stdout.splitlines()[-1] == f"ARG={MODULES['service']}"


def test_the_unit_belongs_to_the_graphical_session():
    unit, install = _unit_section("Unit"), _unit_section("Install")
    assert unit["PartOf"] == ["graphical-session.target"]
    assert unit["After"] == ["graphical-session.target"]
    assert install["WantedBy"] == ["graphical-session.target"]


def test_the_unit_restarts_on_failure_only_and_within_a_bound():
    service, unit = _unit_section("Service"), _unit_section("Unit")
    assert service["Restart"] == ["on-failure"]
    # The program does not call sd_notify (docs/service.md, section 7): notify would never be ready.
    assert service["Type"] == ["simple"]
    assert service["SyslogIdentifier"] == ["slideshow-lock"]
    assert re.fullmatch(r"\d+", service["RestartSec"][0]) and int(service["RestartSec"][0]) >= 1
    # The start limit stops a crash loop: the interval holds more starts than the burst allows.
    assert int(unit["StartLimitBurst"][0]) * int(service["RestartSec"][0]) < int(
        unit["StartLimitIntervalSec"][0]
    )
    # Status 2 (bad option, missing schema) fails the same way again, so it is not retried.
    assert service["RestartPreventExitStatus"] == ["2"]


# -- slideshowlock: the short command that starts the service and opens the settings window -------


@pytest.mark.spawns_processes
def test_the_short_command_runs_the_control_command(short_launcher):
    result = _run(short_launcher)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["ARGC=1", "ARG=control"]


@pytest.mark.spawns_processes
def test_the_login_start_reaches_the_control_module_isolated_from_the_current_directory(
    short_launcher, tmp_path
):
    """The whole way of the autostart entry: slideshowlock -> slideshow-lock control -> the module,
    run as ``/usr/bin/python3 -P -m`` (no current directory in ``sys.path``), with the mode."""
    launcher_text = LAUNCHER.read_text()
    assert launcher_text.count(EXEC_LINE) == 1
    recorder = tmp_path / "python3"
    recorder.write_text('#!/bin/sh\nfor a in "$@"; do echo "ARG=$a"; done\n')
    recorder.chmod(recorder.stat().st_mode | stat.S_IXUSR)
    chain = tmp_path / "slideshow-lock"
    chain.write_text(launcher_text.replace("/usr/bin/python3", str(recorder)))
    short = tmp_path / "slideshowlock"
    short.write_text(SHORT_LAUNCHER.read_text().replace("/usr/bin/slideshow-lock", f"{SH} {chain}"))
    result = _run(short, "autostart")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "ARG=-P",
        "ARG=-m",
        "ARG=slideshow_lock.control",
        "ARG=autostart",
    ]


@pytest.mark.spawns_processes
def test_the_short_command_passes_the_arguments_on_unchanged_and_in_order(short_launcher):
    result = _run(short_launcher, "--debug", "--folder", "/x y")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "ARGC=4",
        "ARG=control",
        "ARG=--debug",
        "ARG=--folder",
        "ARG=/x y",
    ]


@pytest.mark.spawns_processes
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_the_short_commands_help_names_it_and_starts_nothing(short_launcher, flag):
    result = _run(short_launcher, flag)
    assert result.returncode == 0
    assert result.stderr == ""
    assert "ARGC=" not in result.stdout
    assert "usage: slideshowlock [autostart] [--debug]" in result.stdout
    assert "slideshow-lock control" in result.stdout
    assert "--debug" in result.stdout


@pytest.mark.spawns_processes
def test_help_after_another_argument_is_the_programs_own(short_launcher):
    result = _run(short_launcher, "--debug", "--help")
    assert result.stdout.splitlines()[-1] == "ARG=--help"


def test_the_usage_of_slideshow_lock_names_the_short_command():
    text = LAUNCHER.read_text()
    assert "slideshowlock" in text.split("usage() {")[1].split("USAGE\n}")[0]


def test_the_spec_installs_and_lists_both_commands():
    text = SPEC.read_text()
    assert re.search(
        r"^install -Dpm 0755 packaging/slideshowlock %\{buildroot\}%\{_bindir\}/slideshowlock$",
        text,
        re.MULTILINE,
    )
    files = text.split("\n%files", 1)[1].split("\n%changelog", 1)[0].splitlines()
    assert "%{_bindir}/%{name}" in files
    assert "%{_bindir}/slideshowlock" in files
