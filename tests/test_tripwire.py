"""The tripwire of ``conftest.py``: a test (and the code it runs) cannot start a program or
use a bus unless it says so. Negative controls, so the fixture is known not to be blind."""

from __future__ import annotations

import os
import subprocess

import pytest
from gi.repository import Gio, GLib

from tests.conftest import ForbiddenCall

CALLS = {
    "os.system": lambda: os.system("true"),
    "os.popen": lambda: os.popen("true"),
    "os.fork": lambda: os.fork(),
    "os.execv": lambda: os.execv("/bin/true", ["true"]),
    "os.execvp": lambda: os.execvp("true", ["true"]),
    "os.execl": lambda: os.execl("/bin/true", "true"),
    "os.spawnl": lambda: os.spawnl(os.P_WAIT, "/bin/true", "true"),
    "os.posix_spawn": lambda: os.posix_spawn("/bin/true", ["true"], {}),
    "subprocess.Popen": lambda: subprocess.Popen(["true"]),
    "subprocess.run": lambda: subprocess.run(["true"], check=False),
    "GLib.spawn_command_line_async": lambda: GLib.spawn_command_line_async("true"),
    "GLib.spawn_command_line_sync": lambda: GLib.spawn_command_line_sync("true"),
    "GLib.spawn_async": lambda: GLib.spawn_async(["/bin/true"]),
    "Gio.bus_get_sync": lambda: Gio.bus_get_sync(Gio.BusType.SESSION, None),
    "Gio.bus_watch_name": lambda: Gio.bus_watch_name(Gio.BusType.SESSION, "x.y", 0, None, None),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_call_that_starts_a_program_or_reaches_a_bus_raises(name):
    with pytest.raises(ForbiddenCall):
        CALLS[name]()


@pytest.mark.spawns_processes
def test_a_test_that_starts_a_program_on_purpose_can_opt_out():
    assert subprocess.run(["true"], check=False).returncode == 0


def test_the_ordinary_calls_the_suite_makes_are_not_in_the_way(tmp_path):
    (tmp_path / "x").write_text("x")
    assert os.listdir(tmp_path) == ["x"]
    assert os.getpid() > 0
