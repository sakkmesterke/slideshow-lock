"""Tests of the About window and of the donation link (``slideshow_lock/about.py``).

No window is made and no connection is opened: ``Adw`` is replaced by stand-ins that record what
the module asks of them. What the real window looks like is not tested here (it needs a display).
"""

from __future__ import annotations

import ast
import re
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from slideshow_lock import _, about, app_display_name, preferences
from slideshow_lock.version import program_version

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


class FakeAbout:
    """Records what ``show_about`` makes of it, for both libadwaita classes."""

    made = []

    def __init__(self, **properties):
        self.properties = properties
        self.links = []
        self.presented = None
        type(self).made.append(self)

    def add_link(self, title, url):
        self.links.append((title, url))

    def present(self, *args):
        self.presented = args


def _fake_class(name):
    return type(name, (FakeAbout,), {"made": []})


def _adw(*names):
    return SimpleNamespace(**{name: _fake_class(name) for name in names})


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


# -- the rows of the window ----------------------------------------------------------------------


def test_the_window_as_shipped_has_the_donation_link_as_its_third_row(monkeypatch):
    def no_connection(*args, **kwargs):
        raise AssertionError("the rows are made without a connection")

    monkeypatch.setattr(socket.socket, "connect", no_connection)
    assert about.links() == [
        ("Project page", "https://github.com/trensoft/slideshow-lock"),
        ("Report an issue", "https://github.com/trensoft/slideshow-lock/issues"),
        ("Support the project", DONATION_BUTTON),
    ]


def test_with_no_donation_link_the_window_has_the_project_and_issue_rows_only(donation):
    donation("")
    assert about.links() == [
        ("Project page", "https://github.com/trensoft/slideshow-lock"),
        ("Report an issue", "https://github.com/trensoft/slideshow-lock/issues"),
    ]


@pytest.mark.parametrize("value", ["", "   ", "<DONATION_URL>", "http://example.org/donate"])
def test_an_empty_blank_or_placeholder_value_adds_no_row_and_no_text(donation, value):
    donation(value)
    rows = about.links()
    assert len(rows) == 2
    assert "Support the project" not in [label for label, _url in rows]
    assert all(value.strip() not in url for _label, url in rows if value.strip())


def test_a_valid_address_adds_the_support_row_last_with_its_label(donation):
    donation("https://donate.example.org/slideshow-lock")
    assert about.links()[-1] == ("Support the project", "https://donate.example.org/slideshow-lock")
    assert len(about.links()) == 3


def test_the_labels_are_translated_through_the_catalog(monkeypatch, donation):
    donation("https://donate.example.org/slideshow-lock")
    monkeypatch.setattr(about, "_", lambda text: "<" + text + ">")
    assert [label for label, _url in about.links()] == [
        "<Project page>",
        "<Report an issue>",
        "<Support the project>",
    ]


# -- which libadwaita class is used --------------------------------------------------------------


def test_the_dialog_is_used_where_libadwaita_has_it_and_presented_over_the_parent(
    monkeypatch, donation
):
    donation("")
    adw = _adw("AboutDialog", "AboutWindow")
    monkeypatch.setattr(about, "Adw", adw)
    parent = object()
    about.show_about(parent)
    assert len(adw.AboutDialog.made) == 1 and adw.AboutWindow.made == []
    dialog = adw.AboutDialog.made[0]
    assert dialog.presented == (parent,)
    assert "transient_for" not in dialog.properties and "modal" not in dialog.properties


def test_the_window_is_used_where_libadwaita_has_no_dialog_and_is_modal_over_the_parent(
    monkeypatch, donation
):
    donation("")
    adw = _adw("AboutWindow")
    monkeypatch.setattr(about, "Adw", adw)
    parent = object()
    about.show_about(parent)
    window = adw.AboutWindow.made[0]
    assert window.properties["transient_for"] is parent and window.properties["modal"] is True
    assert window.presented == ()


@pytest.mark.parametrize("classes", [("AboutDialog",), ("AboutWindow",)])
def test_both_classes_get_the_same_properties_and_links(monkeypatch, donation, classes):
    donation("https://donate.example.org/slideshow-lock")
    adw = _adw(*classes)
    monkeypatch.setattr(about, "Adw", adw)
    about.show_about(object())
    shown = getattr(adw, classes[0]).made[0]
    properties = shown.properties
    assert properties["application_name"] == app_display_name() == "Slideshow Lock"
    assert properties["application_icon"] == "io.github.trensoft.slideshowlock"
    assert properties["developer_name"] == "TrenSoft"
    assert properties["version"] == program_version()
    assert properties["copyright"] == "© 2026 TrenSoft"
    assert properties["license_type"] == about.Gtk.License.GPL_3_0
    assert properties["comments"] == about.comments()
    # the address of the project and of the issues are links of ours, not the two properties of
    # libadwaita, so that their labels are in the catalogs
    assert "website" not in properties and "issue_url" not in properties
    assert shown.links == about.links()
    assert shown.links[-1] == ("Support the project", "https://donate.example.org/slideshow-lock")


def test_an_empty_donation_link_reaches_neither_class(monkeypatch, donation):
    donation("")
    adw = _adw("AboutDialog")
    monkeypatch.setattr(about, "Adw", adw)
    about.show_about(object())
    shown = adw.AboutDialog.made[0]
    assert [title for title, _url in shown.links] == ["Project page", "Report an issue"]


# -- the texts -----------------------------------------------------------------------------------


def _readme_section(text, heading):
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return match.group(1).strip() if match else None


def test_the_ai_sentence_is_the_sentence_of_the_readme_word_for_word():
    sentence = _readme_section(README.read_text(encoding="utf-8"), "Authorship")
    assert sentence == "The code of this project was written with the help of AI agents."
    assert about.comments().endswith("\n\n" + sentence)


def test_the_description_is_the_two_sentences_of_the_about_text():
    assert about.comments().startswith(
        "Fullscreen slideshow screensaver for GNOME. Any input after idle locks the session."
    )


def test_the_copyright_is_the_holder_and_year_of_the_spdx_headers():
    headers = set()
    for path in sorted(PACKAGE.glob("*.py")):
        match = re.search(r"SPDX-FileCopyrightText: (.*)", path.read_text(encoding="utf-8"))
        assert match, path.name
        headers.add(match.group(1).strip())
    assert headers == {"2026 TrenSoft"}
    assert about.COPYRIGHT == "© " + headers.pop()


def test_the_window_names_the_company_as_the_developer_and_the_holder():
    assert about.DEVELOPER == "TrenSoft"
    assert about.COPYRIGHT.endswith(" " + about.DEVELOPER)


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
    import inspect

    return inspect.getsource(preferences.PreferencesWindow.__init__)


def test_the_main_menu_has_one_entry_that_opens_the_about_window():
    source = _constructor_source()
    action = re.search(r'Gio\.SimpleAction\.new\("([a-z-]+)"', source).group(1)
    assert re.search(rf'menu\.append\(_\("About Slideshow Lock"\), "win\.{action}"\)', source)
    assert "about.show_about(self)" in source and "self.add_action(" in source
    assert source.count("menu.append(") == 1
    assert "header.pack_end(self.menu_button)" in source


def test_nothing_in_the_settings_window_opens_a_connection_for_the_about_window():
    source = (PACKAGE / "about.py").read_text(encoding="utf-8")
    for word in ("socket", "urllib.request", "http.client", "Gio.Subprocess", "subprocess"):
        assert word not in source, word


def test_the_strings_of_the_window_are_marked_for_translation():
    source = (PACKAGE / "about.py").read_text(encoding="utf-8")
    for text in (
        "Project page",
        "Report an issue",
        "Support the project",
        "Fullscreen slideshow screensaver for GNOME. Any input after idle locks the session.",
        "The code of this project was written with the help of AI agents.",
    ):
        assert f'_("{text}")' in source or f'_(\n            "{text}")' in source, text
    assert _("Project page") == "Project page"  # no catalog in a test: the English text
