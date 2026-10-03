"""The tripwire of ``conftest.py``: a test (and the code it runs) cannot start a program or
use a bus unless it says so. Negative controls, so the fixture is known not to be blind."""

from __future__ import annotations

import os
import subprocess

import pytest
from gi.repository import Gio, GLib

from tests.conftest import ForbiddenCall

# The exec controls name a program that does not exist: with the tripwire switched off, the call
# fails with FileNotFoundError and the test fails; with a real program (``/bin/true``) it would
# replace the pytest process itself, and the run would end with a cut-off report and exit 0.
CALLS = {
    "os.system": lambda: os.system("true"),
    "os.popen": lambda: os.popen("true"),
    "os.fork": lambda: os.fork(),
    "os.execv": lambda: os.execv("/nonexistent/x", ["x"]),
    "os.execvp": lambda: os.execvp("/nonexistent/x", ["x"]),
    "os.execl": lambda: os.execl("/nonexistent/x", "x"),
    "os.spawnl": lambda: os.spawnl(os.P_WAIT, "/bin/true", "true"),
    "os.posix_spawn": lambda: os.posix_spawn("/bin/true", ["true"], {}),
    "subprocess.Popen": lambda: subprocess.Popen(["true"]),
    "subprocess.run": lambda: subprocess.run(["true"], check=False),
    "GLib.spawn_command_line_async": lambda: GLib.spawn_command_line_async("true"),
    "GLib.spawn_command_line_sync": lambda: GLib.spawn_command_line_sync("true"),
    "GLib.spawn_async": lambda: GLib.spawn_async(["/bin/true"]),
    "Gio.bus_get_sync": lambda: Gio.bus_get_sync(Gio.BusType.SESSION, None),
    "Gio.bus_watch_name": lambda: Gio.bus_watch_name(Gio.BusType.SESSION, "x.y", 0, None, None),
    "Gio.Subprocess.new": lambda: Gio.Subprocess.new(["true"], Gio.SubprocessFlags.NONE),
    "Gio.Subprocess.newv": lambda: Gio.Subprocess.newv(["true"], Gio.SubprocessFlags.NONE),
    "Gio.SubprocessLauncher.spawnv": lambda: Gio.SubprocessLauncher.new(
        Gio.SubprocessFlags.NONE
    ).spawnv(["true"]),
    "Gio.AppInfo.create_from_commandline": lambda: Gio.AppInfo.create_from_commandline(
        "true", None, Gio.AppInfoCreateFlags.NONE
    ),
    "Gio.AppInfo.launch_default_for_uri": lambda: Gio.AppInfo.launch_default_for_uri(
        "file:///nonexistent/x", None
    ),
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


#: The tests that start a program on purpose. A new one is a decision, made here.
OPT_OUTS = {
    ("test_tripwire.py", "test_a_test_that_starts_a_program_on_purpose_can_opt_out"),
    (
        "test_image_source_gio.py",
        "test_gio_watch_exhaustion_is_reported_once_and_the_walk_still_completes",
    ),
    (
        "test_scaling_gdk.py",
        "test_preparing_a_picture_costs_about_the_same_bytes_per_pixel_for_every_width",
    ),
    ("test_run_script.py", "test_run_sh_is_executable_in_git"),
    ("test_run_script.py", "test_without_arguments_it_prints_the_usage_and_fails"),
    ("test_run_script.py", "test_an_unknown_command_prints_the_usage_and_fails"),
    (
        "test_run_script.py",
        "test_check_names_every_missing_item_and_marks_the_package_names_unverified",
    ),
    ("test_run_script.py", "test_check_without_python3_says_so"),
    ("test_run_script.py", "test_check_fails_without_a_wayland_session"),
    ("test_run_script.py", "test_check_passes_when_everything_is_installed"),
    (
        "test_run_script.py",
        "test_preview_compiles_the_schema_outside_the_checkout_and_passes_the_arguments",
    ),
    (
        "test_run_script.py",
        "test_preview_without_the_dependencies_stops_with_the_report_and_no_traceback",
    ),
    ("test_run_script.py", "test_preview_in_a_checkout_without_the_schema_says_so"),
    ("test_run_script.py", "test_settings_without_the_window_module_says_so_and_is_no_traceback"),
    ("test_run_script.py", "test_settings_starts_the_window_module"),
}


def test_only_the_known_tests_stand_the_tripwire_down(request):
    users = {
        (item.path.name, item.originalname)
        for item in request.session.items
        if item.get_closest_marker("spawns_processes")
    }
    assert users <= OPT_OUTS, f"new opt-out: {sorted(users - OPT_OUTS)}"
