"""Tests of the donation link (``slideshow_lock/about.py``). No window is made and no connection is
opened: the module has no GUI code since the About window went."""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from slideshow_lock import about, preferences

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "slideshow_lock"
README = REPO / "README.md"

VALID = (
    "https://github.com/sponsors/trensoft",
    "https://example.org/donate?campaign=1&x=%20y",
    "https://donate.example.org:8443/slideshow-lock",
    "https://a.b",
)
#: The one address the constant may hold in this release; a change of it is a change of this line.
DONATION_BUTTON = "https://www.paypal.com/donate/?hosted_button_id=QPJCYDA6UXDEJ"
NOT_LINKS = (
    "",
    " ",
    "   \t\n",
    "<DONATION_URL>",
    "https://<DONATION_URL>",
    "https://example.org/<DONATION_URL>",
    "DONATION_URL",
    "http://example.org/donate",
    "HTTPS://example.org/donate",
    "ftp://example.org/donate",
    "javascript:alert(1)",
    "example.org/donate",
    "https://",
    "https://localhost/donate",
    "https://example.org/donate now",
    " https://example.org/donate",
    "https://example.org/donate ",
    "https://example.org/donate\n",
    "https://user@example.org/donate",
    "https://user:secret@example.org/donate",
    "https://example.org:port/donate",
    "https://exa_mple.org/donate",
    "https://exa!mple.org/donate",
    "https://-example.org/donate",
    "https://example.org-/donate",
    "https://[::1]/donate",
    "https://example.org/dönate",
    'https://example.org/"x"',
    42,
)


@pytest.fixture
def donation(monkeypatch):
    def set_to(value):
        monkeypatch.setattr(about, "DONATION_URL", value)

    return set_to


# -- what counts as a link -----------------------------------------------------------------------


@pytest.mark.parametrize("value", VALID)
def test_an_https_address_is_a_donation_link(value):
    assert about.donation_link(value) == value


@pytest.mark.parametrize("value", NOT_LINKS)
def test_everything_else_is_not_a_donation_link(value):
    assert about.donation_link(value) is None


@pytest.mark.parametrize("constant", ["", None, 42, "http://example.org/donate"])
def test_no_value_means_the_constant_and_the_constant_must_be_a_link_too(donation, constant):
    donation(constant)
    assert about.donation_link() is None
    assert about.donation_link(None) is None


def test_the_donation_link_is_the_one_donation_button():
    assert about.DONATION_URL == DONATION_BUTTON
    assert about.donation_link() == DONATION_BUTTON


# -- the README and the maker --------------------------------------------------------------------


def _readme_section(text, heading):
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return match.group(1).strip() if match else None


def test_the_maker_is_the_company_of_the_spdx_headers():
    headers = set()
    for path in sorted(PACKAGE.glob("*.py")):
        match = re.search(r"SPDX-FileCopyrightText: (.*)", path.read_text(encoding="utf-8"))
        assert match, path.name
        headers.add(match.group(1).strip())
    assert headers == {"2026 TrenSoft"}
    assert about.DEVELOPER == "TrenSoft"
    assert headers.pop().endswith(" " + about.DEVELOPER)


# -- the one place of the link -------------------------------------------------------------------


def _assignments_of_the_key():
    """(file, line) of every assignment to the name ``DONATION_URL`` in the package."""
    found = []
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "DONATION_URL":
                    found.append((path.name, node.lineno))
    return found


def test_the_donation_url_is_assigned_in_one_place_and_named_in_one_module():
    assert [name for name, _line in _assignments_of_the_key()] == ["about.py"]
    naming = [
        path.name
        for path in sorted(PACKAGE.glob("*.py"))
        if "DONATION_URL" in path.read_text(encoding="utf-8")
    ]
    assert naming == ["about.py"]


def test_the_key_search_sees_a_second_assignment():
    tree = ast.parse("DONATION_URL = 'x'\nother = 1\nDONATION_URL += 'y'\n")
    names = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AugAssign))
        and (node.targets[0] if isinstance(node, ast.Assign) else node.target).id == "DONATION_URL"
    ]
    assert len(names) == 2  # the shape the search above looks for


def test_no_other_file_that_ships_names_the_donation_link():
    """The data templates, the spec and the packaging files carry no donation address of their
    own: the link is the constant and the README section, nothing else."""
    for path in [*REPO.glob("data/*.in"), *REPO.glob("packaging/**/*")]:
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "DONATION_URL" not in text, path
            assert DONATION_BUTTON not in text, path
            assert not re.search(r"type=\"donation\"", text), path


# -- the entry in the settings window ------------------------------------------------------------


def _constructor_source():
    return inspect.getsource(preferences.PreferencesWindow.__init__)


def test_the_settings_window_has_no_way_into_the_about_window_since_1_0_10():
    """The main menu and its action are gone, and so is ``show_about``: the module has no window."""
    source = _constructor_source()
    assert not hasattr(about, "show_about")
    assert "show_about" not in source and "win.about" not in source
    assert "MenuButton" not in source and "Gio.Menu" not in source
    assert "show_about" not in inspect.getsource(preferences)


def test_nothing_in_the_settings_window_opens_a_connection_for_the_about_window():
    source = (PACKAGE / "about.py").read_text(encoding="utf-8")
    for word in ("socket", "urllib.request", "http.client", "Gio.Subprocess", "subprocess"):
        assert word not in source, word
