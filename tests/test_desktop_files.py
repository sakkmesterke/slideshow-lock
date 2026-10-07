"""The launcher, the AppStream metadata and the icons in ``data/``: they name the application id,
the command and the settings window the same way as the code does.

``data/<APP_ID>.desktop.in`` and ``data/<APP_ID>.metainfo.xml.in`` are templates: the installed
files come out of ``tools/i18n.sh data`` (tested in ``test_i18n_tools.py``). These tests read the
templates and the icon files as text. They do not run ``desktop-file-validate`` or ``appstreamcli``
(the schema of both files is theirs to check); they check the names that bind the files to each
other. The checks are plain functions of the text, so the last group feeds them broken text and
expects each to complain.
"""

from __future__ import annotations

import configparser
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from slideshow_lock import APP_ID, control

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
DESKTOP_IN = DATA / f"{APP_ID}.desktop.in"
METAINFO_IN = DATA / f"{APP_ID}.metainfo.xml.in"
ICONS = DATA / "icons" / "hicolor"
COLOR_ICON = ICONS / "scalable" / "apps" / f"{APP_ID}.svg"
SYMBOLIC_ICON = ICONS / "symbolic" / "apps" / f"{APP_ID}-symbolic.svg"
AUTOSTART_DESKTOP = DATA / f"{APP_ID}.autostart.desktop"
PREFERENCES_PY = REPO / "slideshow_lock" / "preferences.py"
SPEC = REPO / "packaging" / "fedora" / "slideshow-lock.spec"
COMMAND = "slideshow-lock"
SHORT_COMMAND = "slideshowlock"  # opens the settings window, packaging/slideshowlock


def desktop_entry(text: str) -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    parser.optionxform = str
    parser.read_string(text)
    return parser["Desktop Entry"]


def desktop_problems(text: str, preferences_text: str) -> list[str]:
    """What is wrong with the names in the ``.desktop`` template; an empty list when nothing is."""
    entry = desktop_entry(text)
    problems = []
    if entry.get("Icon") != APP_ID:
        problems.append("Icon is not the application id")
    if entry.get("Exec") != SHORT_COMMAND:
        problems.append("Exec does not start the short command")
    if entry.get("TryExec") != SHORT_COMMAND:
        problems.append("TryExec is not the short command")
    wm_class = entry.get("StartupWMClass", "")
    if f'application_id=APP_ID + "{wm_class.removeprefix(APP_ID)}"' not in preferences_text:
        problems.append("StartupWMClass is not the application id of the settings window")
    if entry.get("NoDisplay", "false").lower() == "true":
        problems.append("NoDisplay hides the launcher")
    if entry.get("Terminal", "false").lower() != "false":
        problems.append("Terminal is not false")
    if "Settings" not in entry.get("Categories", "").split(";"):
        problems.append("Categories has no Settings")
    return problems


def autostart_problems(text: str) -> list[str]:
    """What is wrong with the autostart entry; an empty list when nothing is. It runs the short
    command by its absolute path (no ``PATH`` search at login) with the one mode ``control``
    knows for it, it is not shown anywhere, and it is for GNOME only."""
    entry = desktop_entry(text)
    problems = []
    if entry.get("Exec") != f"/usr/bin/{SHORT_COMMAND} {control.AUTOSTART}":
        problems.append("Exec is not the short command's login start")
    if entry.get("TryExec") != f"/usr/bin/{SHORT_COMMAND}":
        problems.append("TryExec is not the short command")
    if entry.get("NoDisplay", "false").lower() != "true":
        problems.append("NoDisplay does not hide the entry")
    if entry.get("OnlyShowIn") != "GNOME;":
        problems.append("OnlyShowIn is not GNOME")
    if entry.get("Type") != "Application" or not entry.get("Name"):
        problems.append("Type or Name is missing")
    for key in ("Hidden", "X-GNOME-Autostart-enabled", "X-GNOME-Autostart-Phase"):
        if key in entry:
            problems.append(f"{key} is set: the entry would be off, or move in the login")
    return problems


def metainfo_problems(text: str) -> list[str]:
    """What is wrong with the names in the metainfo template; an empty list when nothing is."""
    root = ET.fromstring(text)
    problems = []
    if root.findtext("id") != APP_ID:
        problems.append("id is not the application id")
    launchable = root.find("launchable[@type='desktop-id']")
    if launchable is None or launchable.text != f"{APP_ID}.desktop":
        problems.append("launchable is not the desktop file")
    if root.findtext("project_license") != "GPL-3.0-or-later":
        problems.append("project_license is not GPL-3.0-or-later")
    if [binary.text for binary in root.findall("provides/binary")] != [COMMAND, SHORT_COMMAND]:
        problems.append("provides is not the two commands")
    for release in root.iterfind("releases/release"):
        # The date is fixed when the release is made; an undated <release> is a validation error
        # of appstreamcli, so the element stays out until then.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release.get("date", "")):
            problems.append("a release has no date")
    return problems


def test_the_templates_are_named_after_the_application_id():
    assert DESKTOP_IN.is_file()
    assert METAINFO_IN.is_file()


def test_the_desktop_template_names_agree_with_the_code():
    assert (
        desktop_problems(
            DESKTOP_IN.read_text(encoding="utf-8"), PREFERENCES_PY.read_text(encoding="utf-8")
        )
        == []
    )


def test_the_autostart_entry_agrees_with_the_short_command():
    assert autostart_problems(AUTOSTART_DESKTOP.read_text(encoding="utf-8")) == []


def test_the_spec_installs_the_autostart_entry_under_the_application_id_as_config():
    text = SPEC.read_text(encoding="utf-8")
    assert re.search(
        r"^install -Dpm 0644 data/%\{app_id\}\.autostart\.desktop \\\n"
        r"    %\{buildroot\}%\{_sysconfdir\}/xdg/autostart/%\{app_id\}\.desktop$",
        text,
        re.MULTILINE,
    )
    files = text.split("\n%files", 1)[1].split("\n%changelog", 1)[0].splitlines()
    assert "%config(noreplace) %{_sysconfdir}/xdg/autostart/%{app_id}.desktop" in files
    # nothing is enabled or preset: the autostart entry is the one way the service starts
    assert not re.search(r"^[^#\n]*(user-preset|systemctl[^\n]* enable)", text, re.MULTILINE)


def test_the_metainfo_template_names_agree_with_the_desktop_file():
    assert metainfo_problems(METAINFO_IN.read_text(encoding="utf-8")) == []


def test_the_icons_are_named_after_the_application_id_and_are_svg():
    for icon, size in ((COLOR_ICON, "128"), (SYMBOLIC_ICON, "16")):
        root = ET.fromstring(icon.read_text(encoding="utf-8"))
        assert root.tag == "{http://www.w3.org/2000/svg}svg", icon
        assert root.get("width") == root.get("height") == size, icon
        assert root.get("viewBox") == f"0 0 {size} {size}", icon


@pytest.mark.parametrize("icon", [COLOR_ICON, SYMBOLIC_ICON], ids=["color", "symbolic"])
def test_an_icon_has_no_script_text_or_external_reference(icon):
    text = icon.read_text(encoding="utf-8").lower()
    for forbidden in ("<script", "<text", "<image", "href=", "onload", "<foreignobject"):
        assert forbidden not in text, (icon.name, forbidden)


# The negative controls: the same checks on broken text.


def _desktop():
    return DESKTOP_IN.read_text(encoding="utf-8"), PREFERENCES_PY.read_text(encoding="utf-8")


def test_a_wrong_icon_name_is_reported():
    desktop, prefs = _desktop()
    broken = desktop.replace(f"Icon={APP_ID}", "Icon=slideshow-lock")
    assert "Icon is not the application id" in desktop_problems(broken, prefs)


def test_a_wrong_exec_and_try_exec_are_reported():
    desktop, prefs = _desktop()
    assert "Exec does not start the short command" in desktop_problems(
        desktop.replace("Exec=slideshowlock", "Exec=slideshow-lock settings"), prefs
    )
    assert "TryExec is not the short command" in desktop_problems(
        desktop.replace("TryExec=slideshowlock", "TryExec=slideshow-lock-settings"), prefs
    )


def test_a_window_class_that_is_not_the_settings_window_is_reported():
    desktop, prefs = _desktop()
    assert "StartupWMClass is not the application id of the settings window" in desktop_problems(
        desktop.replace(".Preferences", ".Settings"), prefs
    )


def test_a_hidden_launcher_is_reported():
    desktop, prefs = _desktop()
    assert "NoDisplay hides the launcher" in desktop_problems(desktop + "NoDisplay=true\n", prefs)


@pytest.mark.parametrize(
    "old, new, problem",
    [
        (
            "Exec=/usr/bin/slideshowlock autostart",
            "Exec=slideshowlock autostart",
            "Exec is not the short command's login start",
        ),
        (
            "Exec=/usr/bin/slideshowlock autostart",
            "Exec=/usr/bin/slideshowlock",
            "Exec is not the short command's login start",
        ),
        (
            "TryExec=/usr/bin/slideshowlock",
            "TryExec=slideshowlock",
            "TryExec is not the short command",
        ),
        ("NoDisplay=true", "NoDisplay=false", "NoDisplay does not hide the entry"),
        ("OnlyShowIn=GNOME;", "OnlyShowIn=GNOME;KDE;", "OnlyShowIn is not GNOME"),
        ("Type=Application\n", "", "Type or Name is missing"),
    ],
)
def test_a_broken_autostart_entry_is_reported(old, new, problem):
    text = AUTOSTART_DESKTOP.read_text(encoding="utf-8")
    assert old in text
    assert problem in autostart_problems(text.replace(old, new))


@pytest.mark.parametrize("extra", ["Hidden=true", "X-GNOME-Autostart-enabled=false"])
def test_an_autostart_entry_that_is_switched_off_is_reported(extra):
    text = AUTOSTART_DESKTOP.read_text(encoding="utf-8") + extra + "\n"
    assert autostart_problems(text)


def test_a_wrong_metainfo_id_and_launchable_are_reported():
    meta = METAINFO_IN.read_text(encoding="utf-8")
    assert "id is not the application id" in metainfo_problems(
        meta.replace(f"<id>{APP_ID}</id>", "<id>slideshow-lock</id>")
    )
    assert "launchable is not the desktop file" in metainfo_problems(
        meta.replace(f"{APP_ID}.desktop", "slideshow-lock.desktop")
    )


def test_a_provided_binary_that_is_not_the_command_is_reported():
    meta = METAINFO_IN.read_text(encoding="utf-8")
    assert "provides is not the two commands" in metainfo_problems(
        meta.replace("<binary>slideshow-lock</binary>", "<binary>slideshow-lock-daemon</binary>")
    )


def test_a_missing_short_command_in_provides_is_reported():
    meta = METAINFO_IN.read_text(encoding="utf-8")
    assert "provides is not the two commands" in metainfo_problems(
        meta.replace("    <binary>slideshowlock</binary>\n", "")
    )


def test_an_undated_release_is_reported():
    meta = METAINFO_IN.read_text(encoding="utf-8").replace(
        "</component>", '<releases><release version="1.0.0"/></releases></component>'
    )
    assert "a release has no date" in metainfo_problems(meta)
