"""Tests for the manual pages of the two commands (``packaging/man``).

A page is only worth shipping if it says what the programs do. The options and their choices are
compared with the ``--help`` of each program (in both directions: an option the page does not
mention, and one the program does not have), the ranges with what the programs accept, and the
paths with the ones the spec and the code use. The pages are text: no ``man`` or ``mandoc`` is
needed (the pages were also run through ``mandoc -Tlint`` and ``groff -ww`` when they were written;
that is not repeated here).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from slideshow_lock import APP_ID, control, preferences, preview_app, sample_pictures, service
from slideshow_lock.preferences_model import INTERVAL_MAX_SECONDS, INTERVAL_MIN_SECONDS

REPO = Path(__file__).resolve().parent.parent
MAN = REPO / "packaging" / "man"
SPEC = REPO / "packaging" / "fedora" / "slideshow-lock.spec"
COMMANDS = ("slideshow-lock", "slideshowlock")

# The program behind each part of slideshow-lock(1), with its parser.
PROGRAMS = {
    "service": service._parse,
    "preview": preview_app._parse,
    "settings": preferences._parse,
}


def _text(name: str) -> str:
    return (MAN / f"{name}.1").read_text(encoding="utf-8")


def _plain(text: str) -> str:
    """The roff source as plain text for a search: ``\\-`` is a hyphen-minus."""
    return text.replace("\\-", "-")


def _subsection(text: str, name: str) -> str:
    """The lines of ``.SS "name"`` up to the next ``.SS`` or ``.SH``."""
    lines, inside = [], False
    for line in text.splitlines():
        if line.startswith((".SS", ".SH")):
            inside = line.split(None, 1)[1].strip('"') == name if line.startswith(".SS") else False
            continue
        if inside:
            lines.append(line)
    return "\n".join(lines)


def _long_options(text: str) -> set:
    return set(re.findall(r"--[a-z][a-z-]*", _plain(text))) - {"--help"}


def _help_of(parse, capsys) -> str:
    with pytest.raises(SystemExit) as stop:
        parse(["--help"])
    assert stop.value.code == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("name", COMMANDS)
def test_each_command_has_a_page_with_the_licence_header_and_its_own_name(name):
    text = _text(name)
    lines = text.splitlines()
    assert lines[:2] == [
        r".\" SPDX-FileCopyrightText: 2026 TrenSoft",
        r".\" SPDX-License-Identifier: GPL-3.0-or-later",
    ]
    assert re.match(rf'\.TH {re.escape(name.upper().replace("-", chr(92) + "-"))} 1 "', lines[2])
    [name_line] = re.findall(r"^\.SH NAME\n(.+)$", text, re.MULTILINE)
    assert name_line.startswith(name.replace("-", r"\-") + r" \- ")


def test_every_command_of_the_package_has_a_page():
    """The commands are the files of ``packaging/`` that the spec installs into ``%{_bindir}``."""
    spec = SPEC.read_text()
    installed = set(
        re.findall(r"^install -Dpm 0755 packaging/(\S+) %\{buildroot\}%\{_bindir\}", spec, re.M)
    )
    installed = {name.replace("%{name}", "slideshow-lock") for name in installed}
    assert installed == set(COMMANDS)
    assert {path.stem for path in MAN.glob("*.1")} == set(COMMANDS)


@pytest.mark.parametrize("name", COMMANDS)
def test_the_spec_installs_and_lists_the_page_with_a_compression_proof_pattern(name):
    spec = SPEC.read_text()
    assert re.search(
        rf"^install -Dpm 0644 packaging/man/{name}\.1 "
        rf"%\{{buildroot\}}%\{{_mandir\}}/man1/{name}\.1$",
        spec,
        re.MULTILINE,
    )
    files = spec.split("\n%files", 1)[1].split("\n%changelog", 1)[0].splitlines()
    assert f"%{{_mandir}}/man1/{name}.1*" in files  # rpm compresses the page: the name has a suffix


@pytest.mark.parametrize("program", sorted(PROGRAMS))
def test_the_options_of_a_program_are_the_options_of_its_part_of_the_page(program, capsys):
    page = _subsection(_text("slideshow-lock"), program)
    program_options = _long_options(_help_of(PROGRAMS[program], capsys))
    assert program_options  # --debug at least
    assert _long_options(page) == program_options


def test_the_page_of_the_short_command_has_the_options_of_the_control_command(capsys):
    options = _long_options(_help_of(control._parse, capsys))
    assert options == {"--debug"}
    for text in (_text("slideshowlock"), _subsection(_text("slideshow-lock"), "control")):
        assert _long_options(text) == options
        assert control.AUTOSTART in text


@pytest.mark.parametrize("program", ["service", "preview"])
@pytest.mark.parametrize("option", ["--order", "--scaling", "--transition"])
def test_the_choices_of_an_option_are_all_in_the_page(program, option, capsys):
    help_text = _help_of(PROGRAMS[program], capsys)
    found = re.search(rf"{option} \{{([^}}]*)\}}", help_text)
    if option == "--transition" and program == "service":
        assert found is None  # the service has no such option, and the page does not say it has
        assert option not in _long_options(_subsection(_text("slideshow-lock"), program))
        return
    assert found, (program, option)
    page = _plain(_subsection(_text("slideshow-lock"), program))
    for choice in found.group(1).split(","):
        assert re.search(rf"(?<![\w-]){re.escape(choice)}(?![\w-])", page), (program, choice)


@pytest.mark.parametrize("program", ["service", "preview"])
def test_the_range_of_the_interval_is_the_one_the_program_accepts(program):
    page = _plain(_subsection(_text("slideshow-lock"), program))
    assert re.search(rf"from\s+{INTERVAL_MIN_SECONDS}\s+to\s+{INTERVAL_MAX_SECONDS}\b", page)
    parse = PROGRAMS[program]
    overrides = {
        "service": service.overrides_from_args,
        "preview": preview_app.overrides_from_args,
    }[program]
    for seconds in (INTERVAL_MIN_SECONDS, INTERVAL_MAX_SECONDS):
        assert overrides(parse(["--interval", str(seconds)]))
    for seconds in (INTERVAL_MIN_SECONDS - 1, INTERVAL_MAX_SECONDS + 1):
        with pytest.raises(ValueError):
            overrides(parse(["--interval", str(seconds)]))


@pytest.mark.parametrize(
    "option,low,high",
    [("--idle-timeout", 1, 86400), ("--grace", 0, 86400)],
)
def test_the_range_of_the_idle_timeout_and_of_the_grace_period_is_the_one_the_service_accepts(
    option, low, high
):
    page = _plain(_subsection(_text("slideshow-lock"), "service"))
    assert re.search(rf"{option}\W+SECONDS.*?from\s+{low}\s+to\s+{high}\b", page, re.DOTALL)
    for good in (low, high):
        assert service.overrides_from_args(service._parse([option, str(good)]))
    for bad in (low - 1, high + 1):
        with pytest.raises(ValueError):
            service.overrides_from_args(service._parse([option, str(bad)]))


def _names(page: str, path: str) -> bool:
    """*path* is in *page* as a whole name (``a.desktop`` is not in ``a.desktopx``)."""
    return re.search(rf"(?<![\w/.-]){re.escape(path)}(?![\w.-])", page) is not None


def test_the_page_names_the_files_where_the_spec_and_the_code_put_them():
    page = _plain(_text("slideshow-lock"))
    autostart = f"/etc/xdg/autostart/{APP_ID}.desktop"
    assert _names(page, autostart)
    assert _names(page, f"/usr/lib/systemd/user/{sample_pictures.APP_DIR}.service")
    assert _names(page, f"/usr/share/{sample_pictures.APP_DIR}/{sample_pictures.DATA_SUBDIR}")
    assert re.search(rf"into the\s+folder\s+\.I {sample_pictures.SUBDIR}\b", page)
    assert _names(_plain(_text("slideshowlock")), autostart)
    spec = SPEC.read_text()
    assert f"%global app_id {APP_ID}\n" in spec
    assert "%{_sysconfdir}/xdg/autostart/%{app_id}.desktop" in spec
    assert "%{_userunitdir}/%{name}.service" in spec


def test_the_path_reader_tells_a_file_from_a_longer_name():
    """Negative control of the test above."""
    assert _names("see /etc/x/a.desktop here", "/etc/x/a.desktop")
    assert _names(".I /etc/x/a.desktop\n", "/etc/x/a.desktop")
    assert not _names("see /etc/x/a.desktopx here", "/etc/x/a.desktop")
    assert not _names("see /etc/x/a.desktop.bak here", "/etc/x/a.desktop")
    assert not _names("see /usr/etc/x/a.desktop here", "/etc/x/a.desktop")


def test_the_option_reader_sees_an_option_the_program_does_not_have():
    """Negative control of the comparisons above: they are set comparisons, in both directions."""
    page = ".SS service\n.B \\-\\-debug\n.B \\-\\-invented\n.SS preview\n.B \\-\\-folder\n"
    assert _long_options(_subsection(page, "service")) == {"--debug", "--invented"}
    assert _long_options(_subsection(page, "preview")) == {"--folder"}
    assert _long_options(_subsection(page, "control")) == set()
