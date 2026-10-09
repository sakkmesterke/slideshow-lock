"""Where the version shown in the settings window comes from (``slideshow_lock.version``)."""

import importlib.metadata
import inspect
import os
import re

import pytest

from slideshow_lock import preferences, version

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tree(tmp_path, pyproject):
    package = tmp_path / "slideshow_lock"
    package.mkdir()
    if pyproject is not None:
        data = pyproject if isinstance(pyproject, bytes) else pyproject.encode("utf-8")
        (tmp_path / "pyproject.toml").write_bytes(data)
    return str(package)


def _no_metadata(monkeypatch):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)


def _metadata(monkeypatch, value):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: value)


PYPROJECT = (
    '[build-system]\nrequires = ["x"]\n\n[project]\nname = "slideshow-lock"\nversion = "7.8.9"\n'
)


def test_a_source_tree_gives_the_version_of_its_pyproject(tmp_path, monkeypatch):
    _metadata(monkeypatch, "1.0.0")  # an older package installed too: the tree beside the code wins
    assert version.program_version(_tree(tmp_path, PYPROJECT)) == "7.8.9"


def test_without_a_source_tree_the_installed_metadata_answers(tmp_path, monkeypatch):
    _metadata(monkeypatch, "2.3.4")
    assert version.program_version(_tree(tmp_path, None)) == "2.3.4"


def test_with_neither_the_answer_is_dev_and_not_an_error(tmp_path, monkeypatch):
    _no_metadata(monkeypatch)
    assert version.program_version(_tree(tmp_path, None)) == "dev"


def test_broken_metadata_is_dev_too(tmp_path, monkeypatch):
    def broken(name):
        raise RuntimeError("damaged dist-info")

    monkeypatch.setattr(importlib.metadata, "version", broken)
    assert version.program_version(_tree(tmp_path, None)) == "dev"


@pytest.mark.parametrize(
    "text",
    [
        '[project]\nname = "another-project"\nversion = "7.8.9"\n',  # not this program's file
        '[project]\nname = "slideshow-lock"\n',  # no version
        '[tool.x]\nversion = "7.8.9"\n[project]\nname = "slideshow-lock"\n',  # another table's
        b"\xff\xfe not utf-8",
    ],
    ids=["other-project", "no-version", "other-table", "not-text"],
)
def test_a_pyproject_that_does_not_say_it_is_skipped(tmp_path, monkeypatch, text):
    _metadata(monkeypatch, "2.3.4")
    assert version.program_version(_tree(tmp_path, text)) == "2.3.4"


def test_an_unreadable_pyproject_is_skipped(tmp_path, monkeypatch):
    _no_metadata(monkeypatch)
    package = _tree(tmp_path, None)
    os.mkdir(tmp_path / "pyproject.toml")  # a folder where the file should be
    assert version.program_version(package) == "dev"


def test_the_real_tree_gives_the_version_pyproject_and_the_spec_state():
    """The one number: pyproject.toml has it, the spec says the same, and the code shows it."""
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as handle:
        written = re.search(r'^version = "([^"]+)"$', handle.read(), re.M).group(1)
    with open(
        os.path.join(ROOT, "packaging", "fedora", "slideshow-lock.spec"), encoding="utf-8"
    ) as handle:
        spec = re.search(r"^Version:\s+(\S+)$", handle.read(), re.M).group(1)
    assert version.program_version() == written == spec


def test_no_version_number_is_written_in_the_code():
    """No second copy to keep: the modules that show the version contain no version literal."""
    for module in (version, preferences):
        source = inspect.getsource(module)
        assert not re.search(r"""["']\d+\.\d+\.\d+["']""", source), module.__name__


def test_the_line_is_the_version_and_the_maker(monkeypatch):
    monkeypatch.setattr(preferences, "program_version", lambda: "1.0.10")
    assert preferences.version_text() == "1.0.10 by TrenSoft"
    monkeypatch.undo()
    assert preferences.version_text() == version.program_version() + " by TrenSoft"


def test_the_window_shows_it_small_and_faint_at_the_end_of_the_footer():
    """The constructor cannot run here, so its statements are read (the real placement is the
    smoke test's): the label is made from ``version_text()`` (the version of the package, "by"
    and the maker), is dim, and is the last thing put in the footer."""
    source = inspect.getsource(preferences.PreferencesWindow.__init__)
    assert "label=version_text()" in source
    assert 'self.version_label.add_css_class("dim-label")' in source
    appended = re.findall(r"footer\.append\(([^)]*)\)", source)
    assert appended == ["self.preview_button", "self.status", "self.version_label"]
