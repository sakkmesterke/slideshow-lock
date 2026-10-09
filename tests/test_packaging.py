"""Tests for the files the package installs next to the program: the launcher and the systemd unit.

``packaging/slideshow-lock`` is run for real, with ``sh``. Its interpreter, ``/usr/bin/python3``, is
replaced in a copy by a program that only records how it was called: the launcher's job is to pick
the module and pass the arguments on, and the real programs need a display and a bus. A test pins
the line that is replaced, so the copy cannot drift from the file that is installed.

The unit is read as text. ``systemd-analyze verify`` is not run here (it needs systemd, which the CI
image has no use for); the unit and the launcher are checked against each other instead.
"""

from __future__ import annotations

import ast
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from slideshow_lock import sample_pictures

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "packaging" / "slideshow-lock"
SHORT_LAUNCHER = REPO / "packaging" / "slideshowlock"
SPEC = REPO / "packaging" / "fedora" / "slideshow-lock.spec"
UNIT = REPO / "data" / "slideshow-lock.service"
METAINFO = REPO / "data" / "io.github.trensoft.slideshowlock.metainfo.xml.in"
CC_LICENSE = REPO / "packaging" / "licenses" / "CC-BY-SA-4.0.txt"
LICENSE_EXPRESSION = "GPL-3.0-or-later AND CC-BY-SA-4.0"
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


# -- the loaders for the picture formats (docs/image-source.md, "Picture formats") ---------------


def _conditions_of(prefix: str) -> list:
    """The spec lines that start with *prefix*, each with the %if conditions open at that line.

    A line outside every conditional gets ``[]``; a line in ``%if 0%{?rhel}`` gets
    ``["0%{?rhel}"]``. Only the dependency block of the spec is read: lines in comments never
    start with a tag, so they are not counted.
    """
    found, open_ifs = [], []
    for line in SPEC.read_text().splitlines():
        word = line.split(None, 1)[0] if line.strip() else ""
        if word in ("%if", "%ifarch", "%ifnarch"):
            open_ifs.append(line.split(None, 1)[1].strip())
        elif word == "%endif":
            open_ifs.pop()
        elif line.startswith(prefix):
            found.append((line.split(":", 1)[1].strip(), list(open_ifs)))
    return found


@pytest.mark.parametrize(
    "tag,package",
    [
        ("Requires:", "gdk-pixbuf2-modules"),
        ("Recommends:", "webp-pixbuf-loader"),
        ("Recommends:", "gdk-pixbuf2-modules-extra"),
    ],
)
def test_the_spec_asks_for_the_picture_loaders_on_rhel_only(tag, package):
    assert (package, ["0%{?rhel}"]) in _conditions_of(tag)
    assert [c for p, c in _conditions_of(tag) if p == package] == [["0%{?rhel}"]]  # once


def test_the_epel_loaders_are_weak_dependencies_and_never_required():
    """The package must not need EPEL: the two packages only EPEL 10 has are Recommends."""
    required = {p for p, _ in _conditions_of("Requires:")}
    assert "gdk-pixbuf2-modules" in required
    assert not required & {"webp-pixbuf-loader", "gdk-pixbuf2-modules-extra"}


def test_the_conditions_helper_sees_an_unconditional_line_and_a_nested_one(tmp_path, monkeypatch):
    """Negative control of the two tests above: the reader tells a bare line from a guarded one."""
    fake = tmp_path / "x.spec"
    fake.write_text(
        "Requires:       bare\n%if 0%{?rhel}\nRequires:       guarded\n%if %{with tests}\n"
        "Requires:       nested\n%endif\n%endif\nRequires:       after\n"
    )
    monkeypatch.setattr("tests.test_packaging.SPEC", fake)
    assert _conditions_of("Requires:") == [
        ("bare", []),
        ("guarded", ["0%{?rhel}"]),
        ("nested", ["0%{?rhel}", "%{with tests}"]),
        ("after", []),
    ]


# -- the sample pictures in the package (the pictures themselves: test_pictures_clean.py) ----------


def _tag(text: str, name: str) -> str:
    [value] = re.findall(rf"^{name}:\s+(.*?)\s*$", text, re.MULTILINE)
    return value


def _section(text: str, name: str) -> list:
    """The lines of the spec section that starts with ``%name`` (up to the next section)."""
    lines, inside = [], False
    for line in text.splitlines():
        head = re.match(r"%([a-z_]+)\b", line)
        if head and head.group(1) in SECTIONS:
            inside = head.group(1) == name
            continue
        if inside:
            lines.append(line)
    return lines


SECTIONS = {
    "description", "prep", "build", "install", "check", "files", "changelog", "package",
    "generate_buildrequires", "pre", "post", "preun", "postun", "pretrans", "posttrans",
}  # fmt: skip


def scriptlet_problems(text: str) -> list:
    """Scriptlet lines that name a home directory: the package must not touch a user's files."""
    problems = []
    for name in ("pre", "post", "preun", "postun", "pretrans", "posttrans"):
        for line in _section(text, name):
            code = line.split("#", 1)[0]
            if re.search(r"\$\{?HOME\b|~|/home\b", code):
                problems.append(f"%{name}: {line.strip()}")
    return problems


def test_the_scriptlets_name_no_home_directory():
    """FR-10: the pictures reach a user's folder through the program at login, never through the
    package (a scriptlet runs as root, for every user, and must not write into a home)."""
    assert _section(SPEC.read_text(), "post"), "the section reader finds no %post"
    assert scriptlet_problems(SPEC.read_text()) == []


@pytest.mark.parametrize(
    "line", ["cp x $HOME/Pictures", "cp x ${HOME}/y", "cp x ~/y", "cp x /home/u/y"]
)
@pytest.mark.parametrize("section", ["pre", "post", "preun", "postun", "posttrans"])
def test_the_scriptlet_check_sees_a_home_directory_in_every_scriptlet(section, line):
    text = SPEC.read_text().replace(f"\n%{section}\n", f"\n%{section}\n{line}\n", 1)
    if section in ("pre", "postun"):  # the spec has no such section: the control adds one
        text = text.replace("\n%post\n", f"\n%{section}\n{line}\n\n%post\n", 1)
    assert scriptlet_problems(text), (section, line)


def test_the_pictures_are_installed_and_listed_where_the_program_looks_for_them():
    """The spec and ``sample_pictures`` name one folder: ``<datadir>/slideshow-lock/pictures``."""
    text = SPEC.read_text()
    assert _tag(text, "Name") == sample_pictures.APP_DIR
    folder = f"%{{_datadir}}/%{{name}}/{sample_pictures.DATA_SUBDIR}"
    assert re.search(rf"^install -d -m 0755 %\{{buildroot\}}{re.escape(folder)}$", text, re.M)
    assert re.search(
        rf"^install -pm 0644 data/pictures/\* %\{{buildroot\}}{re.escape(folder)}/$", text, re.M
    )
    files = _section(text, "files")
    assert "%dir %{_datadir}/%{name}" in files  # nothing else owns /usr/share/slideshow-lock
    assert f"%dir {folder}" in files
    assert f"{folder}/*" in files
    assert sample_pictures.DATA_DIRS == ("/usr/local/share", "/usr/share")


def test_the_licence_of_the_package_is_the_code_and_the_pictures_in_the_spec_and_the_metainfo():
    assert _tag(SPEC.read_text(), "License") == LICENSE_EXPRESSION
    meta = METAINFO.read_text()
    assert f"<project_license>{LICENSE_EXPRESSION}</project_license>" in meta
    assert "<metadata_license>CC0-1.0</metadata_license>" in meta


def test_the_licence_text_is_listed_once_and_matches_no_default_pattern_of_setuptools():
    files = _section(SPEC.read_text(), "files")
    assert files.count("%license packaging/licenses/CC-BY-SA-4.0.txt") == 1
    assert len([line for line in files if line.startswith("%license")]) == 1
    text = CC_LICENSE.read_text(encoding="utf-8")
    assert text.startswith("Attribution-ShareAlike 4.0 International")
    assert "Creative Commons" in text and "ShareAlike" in text
    assert not (REPO / CC_LICENSE.name).exists()  # not in the root, where setuptools looks
    for pattern in ("LICEN[CS]E*", "COPYING*", "NOTICE*", "AUTHORS*"):
        assert not CC_LICENSE.match(pattern)
    assert "license-files" not in (REPO / "pyproject.toml").read_text()


def test_the_changelog_names_the_pictures_and_their_licence():
    changelog = SPEC.read_text().split("\n%changelog\n", 1)[1]
    entry = changelog.split("- 1.0.1-1\n", 1)[1].split("\n\n", 1)[0]
    assert "CC BY-SA 4.0" in entry and "License tag" in entry


# -- the running user service is restarted after an upgrade ----------------------------------------


def _code(lines: list) -> list:
    return [line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()]


def test_an_upgrade_restarts_the_running_user_service():
    """ "sudo dnf upgrade" must leave nothing to do by hand: the new package marks the unit for a
    restart in the user managers, and the systemd package restarts what is marked at the end of the
    transaction. The macro is in %posttrans, the scriptlet of the NEW package: on an upgrade the
    %postun that runs is the old package's, and the old one (1.0.1) has none."""
    text = SPEC.read_text()
    assert _code(_section(text, "posttrans")) == [
        "%systemd_user_posttrans_with_restart %{name}.service"
    ]


def test_the_restart_is_not_in_postun_where_the_first_upgrade_would_miss_it():
    text = SPEC.read_text()
    assert _section(text, "postun") == []
    assert "%systemd_user_postun" not in "\n".join(_code(text.splitlines()))


def test_the_other_user_unit_scriptlets_stay():
    text = SPEC.read_text()
    assert _code(_section(text, "post")) == ["%systemd_user_post %{name}.service"]
    assert _code(_section(text, "preun")) == ["%systemd_user_preun %{name}.service"]


@pytest.mark.parametrize(
    "stale",
    [
        "%%systemd_user_postun is empty",
        "the upgrade does not\n#     restart the running service",
        "Whether it should is a\n#     decision, not taken here",
    ],
)
def test_the_spec_comments_no_longer_say_the_upgrade_leaves_the_service_running(stale):
    """The comments said the opposite of the scriptlets once ("decision, not taken here")."""
    assert stale not in SPEC.read_text()


# -- the SPDX header of the sources (the licence of the code, in the code) ----------------------

HOLDER = "TrenSoft"
SPDX_LINES = [
    f"# SPDX-FileCopyrightText: 2026 {HOLDER}",
    "# SPDX-License-Identifier: GPL-3.0-or-later",
]
HEADED = sorted(
    [*(REPO / "slideshow_lock").glob("*.py"), LAUNCHER, SHORT_LAUNCHER],
    key=lambda path: path.name,
)


def _header_problem(text: str):
    """None if *text* has the two SPDX lines right after an optional shebang, else what is wrong."""
    lines = text.splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    if lines[:2] != SPDX_LINES:
        return f"first lines {lines[:2]!r}"
    if len(lines) < 3 or lines[2] != "":
        return "no empty line after the header"
    return None


@pytest.mark.parametrize("path", HEADED, ids=lambda path: path.name)
def test_every_module_and_launcher_carries_the_two_spdx_lines(path):
    """licensecheck of the Fedora review marked every file "Unknown" when the sources had no
    header; the shebang of a launcher stays the first line."""
    assert _header_problem(path.read_text(encoding="utf-8")) is None


def test_the_headed_files_are_all_of_the_package_and_both_commands():
    assert len(HEADED) == len(list((REPO / "slideshow_lock").glob("*.py"))) + 2
    assert {p.name for p in HEADED} >= {"__init__.py", "slideshow-lock", "slideshowlock"}


@pytest.mark.parametrize(
    "text,problem",
    [
        ("#!/bin/sh\n" + "\n".join(SPDX_LINES) + "\n\nx\n", None),
        ("\n".join(SPDX_LINES) + '\n\n"""doc"""\n', None),
        ('"""doc"""\n', "first lines"),
        ("#!/bin/sh\n" + "\n".join(SPDX_LINES[::-1]) + "\n\nx\n", "first lines"),
        ("\n".join(SPDX_LINES).replace("2026", "2025") + "\n\nx\n", "first lines"),
        ("\n".join(SPDX_LINES) + "\nx\n", "no empty line"),
        ("\n".join(SPDX_LINES).replace("-or-later", "-only") + "\n\nx\n", "first lines"),
    ],
)
def test_the_header_check_sees_a_missing_wrong_or_misplaced_header(text, problem):
    """Negative control of the test above."""
    found = _header_problem(text)
    assert (found is None) if problem is None else (found is not None and problem in found)


def test_the_holder_of_the_header_is_the_developer_of_the_metainfo_and_the_packager_of_the_spec():
    """One spelling of the name everywhere: the header, the AppStream developer, the %changelog."""
    developer = re.search(r"<developer[^>]*>\s*<name[^>]*>([^<]+)</name>", METAINFO.read_text())
    assert developer and developer.group(1) == HOLDER
    entries = re.findall(r"^\* \w{3} \w{3} \d\d \d{4} (.+?) <", SPEC.read_text(), re.MULTILINE)
    assert entries and set(entries) == {HOLDER}
    assert SPDX_LINES[0].endswith(HOLDER)


# -- the validators of the metainfo, and the version of libadwaita ---------------------------------

ADW_1_2 = {
    "Application",
    "ApplicationWindow",
    "HeaderBar",
    "Clamp",
    "PreferencesGroup",
    "PreferencesRow",
    "ActionRow",
    "ComboRow",
    "EntryRow",  # the newest of the rows: libadwaita 1.2
    "AboutWindow",  # libadwaita 1.2, deprecated from 1.6
}

#: A class newer than the required libadwaita, with the version that has it. The code may use it
#: only behind ``hasattr(Adw, "<Name>")``, with the older class as the fallback
#: (``test_a_newer_libadwaita_class_is_used_only_behind_hasattr``).
ADW_OPTIONAL: dict = {}  # none in use since the About window went; the guard tests below stay


def test_the_build_asks_for_appstream_util_and_runs_validate_relax_on_the_installed_metainfo():
    """AppData guideline of Fedora: validate-relax, with libappstream-glib as a BuildRequires. The
    package is in the repositories of all four chroots, so the line is not conditional."""
    assert ("libappstream-glib", []) in _conditions_of("BuildRequires:")
    check = _code(_section(SPEC.read_text(), "check"))
    assert (
        "appstream-util validate-relax --nonet "
        "%{buildroot}%{_metainfodir}/%{app_id}.metainfo.xml" in check
    )
    assert any(line.startswith("appstreamcli validate") for line in check)  # it stays next to it


def test_libadwaita_is_required_with_the_version_the_window_code_needs():
    """An unversioned Requires on a library-like name is rpmlint's explicit-lib-dependency error."""
    required = [(p, c) for p, c in _conditions_of("Requires:") if p.startswith("libadwaita")]
    assert required == [("libadwaita >= 1.2", [])]
    assert "No version is required here" not in SPEC.read_text()


def _adw_names() -> set:
    """Every ``Adw.<Name>`` the modules of the package use."""
    names = set()
    for path in (REPO / "slideshow_lock").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "Adw"
            ):
                names.add(node.attr)
    return names


def test_the_code_uses_no_libadwaita_class_that_is_newer_than_the_required_version():
    """The number in the spec (1.2) is the version of Adw.EntryRow. A newer class (SwitchRow and
    SpinRow 1.4, Dialog 1.5, PreferencesDialog 1.5 ...) needs a higher number there first."""
    assert _adw_names() <= ADW_1_2 | set(ADW_OPTIONAL)
    assert "EntryRow" in _adw_names()  # the class that sets the number is still in use


def _unguarded_uses(source: str, name: str) -> list:
    """The lines where ``Adw.<name>`` is used outside an ``if hasattr(Adw, "<name>"):`` body."""
    tree = ast.parse(source)
    guarded = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Call)
            and isinstance(node.test.func, ast.Name)
            and node.test.func.id == "hasattr"
            and [ast.dump(a) for a in node.test.args]
            == [ast.dump(ast.Name(id="Adw", ctx=ast.Load())), ast.dump(ast.Constant(name))]
        ):
            for statement in node.body:
                guarded.update(id(child) for child in ast.walk(statement))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "Adw"
        and node.attr == name
        and id(node) not in guarded
    ]


def test_a_newer_libadwaita_class_is_used_only_behind_hasattr():
    """``Adw.AboutDialog`` (1.5) is used where libadwaita has it; the Requires stays at 1.2."""
    for name in ADW_OPTIONAL:
        used = False
        for path in (REPO / "slideshow_lock").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            used = used or f"Adw.{name}" in source
            assert _unguarded_uses(source, name) == [], (path.name, name)
        assert used, f"{name} is no longer used: take it off ADW_OPTIONAL"


def test_the_guard_reader_sees_an_unguarded_use_of_a_newer_class():
    guarded = 'if hasattr(Adw, "AboutDialog"):\n    d = Adw.AboutDialog()\n'
    assert _unguarded_uses(guarded, "AboutDialog") == []
    assert _unguarded_uses("d = Adw.AboutDialog()\n", "AboutDialog") == [1]
    assert _unguarded_uses(guarded + "e = Adw.AboutDialog()\n", "AboutDialog") == [3]
    assert _unguarded_uses(
        'if hasattr(Adw, "Other"):\n    d = Adw.AboutDialog()\n', "AboutDialog"
    ) == [2]
    assert _unguarded_uses(
        'if not hasattr(Adw, "AboutDialog"):\n    d = Adw.AboutDialog()\n', "AboutDialog"
    ) == [2]


def test_the_class_reader_sees_a_class_the_list_does_not_have(tmp_path, monkeypatch):
    """Negative control of the test above."""
    package = tmp_path / "slideshow_lock"
    package.mkdir()
    (package / "x.py").write_text("from gi.repository import Adw\nrow = Adw.SwitchRow()\n")
    monkeypatch.setattr("tests.test_packaging.REPO", tmp_path)
    assert _adw_names() == {"SwitchRow"}


def test_there_is_no_rpmlintrc_for_a_message_the_spec_no_longer_causes():
    """The only filter it had (explicit-lib-dependency libadwaita) went with the versioned Requires,
    and the Fedora configuration of rpmlint reports a filter that matches nothing as an error
    (unused-rpmlintrc-filter)."""
    assert list(SPEC.parent.glob("*.rpmlintrc")) == []
