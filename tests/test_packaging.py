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
UNIT = REPO / "data" / "slideshow-lock.service"
SH = shutil.which("sh") or "/bin/sh"
EXEC_LINE = 'exec /usr/bin/python3 -P -m "$module" "$@"'

MODULES = {
    "service": "slideshow_lock.service",
    "settings": "slideshow_lock.settings_app",
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
