"""What the source asks the translators for: every ``_()`` takes one plain string literal (so that
xgettext can find it) and runs inside a function (so that it runs after the language is chosen),
and the template holds exactly those strings.

The reader of the source is ``tools/i18n_catalog.py``, the one that ``tools/i18n.sh check`` uses.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "slideshow_lock"

_spec = importlib.util.spec_from_file_location("i18n_catalog", REPO / "tools" / "i18n_catalog.py")
catalog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(catalog)


def _source(tmp_path, text):
    (tmp_path / "module.py").write_text(text, encoding="utf-8")
    return tmp_path


def test_the_package_has_gettext_calls_and_every_one_is_a_plain_literal_inside_a_function():
    calls = catalog.gettext_calls(PACKAGE)
    assert len(calls) > 70, "the reader no longer finds the calls"
    assert [c for c in calls if c.msgid is None] == []
    assert [c for c in calls if c.at_import] == []


@pytest.mark.parametrize(
    "text",
    [
        "def f(name):\n    return _(name)\n",
        'def f(name):\n    return _(f"hello {name}")\n',
        'def f(name):\n    return _("hello " + name)\n',
        'def f(name):\n    return _("hello %s") % name and _("a", "b")\n',
        'def f(name):\n    return _("a", context=name)\n',
        "def f(name):\n    return _()\n",
    ],
)
def test_a_call_that_is_not_one_plain_literal_is_reported(tmp_path, text):
    problems = catalog.compare(_pot(tmp_path), _source(tmp_path, text))
    assert any("plain string literal" in problem for problem in problems)


@pytest.mark.parametrize(
    "text",
    [
        'LABEL = _("module level")\n',
        'def f(label=_("default value")):\n    return label\n',
        '@decorate(_("decorator"))\ndef f():\n    pass\n',
        'class A:\n    label = _("class body")\n',
        'def f():\n    pass\n\nvalue = [_("in a list")]\n',
    ],
)
def test_a_call_that_runs_when_the_module_is_imported_is_reported(tmp_path, text):
    problems = catalog.compare(_pot(tmp_path), _source(tmp_path, text))
    assert any("runs at import" in problem for problem in problems)


@pytest.mark.parametrize(
    "text",
    [
        'def f():\n    return _("in a function")\n',
        'def f():\n    def g():\n        return _("nested")\n    return g\n',
        'class A:\n    def m(self):\n        return _("in a method")\n',
        'def f():\n    return lambda: _("in a lambda")\n',
    ],
)
def test_a_call_inside_a_function_is_not_reported(tmp_path, text):
    (call,) = catalog.gettext_calls(_source(tmp_path, text))
    assert call.msgid is not None and not call.at_import


def test_the_reader_goes_into_subdirectories(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.py").write_text('def f():\n    return _("deep")\n')
    assert [call.msgid for call in catalog.gettext_calls(tmp_path)] == ["deep"]


def _pot(tmp_path, *msgids):
    path = tmp_path / "messages.pot"
    entries = ['msgid ""\nmsgstr ""\n"Project-Id-Version: x\\n"\n']
    entries += ['msgid "%s"\nmsgstr ""\n' % msgid for msgid in msgids]
    path.write_text("\n".join(entries), encoding="utf-8")
    return path


def test_the_template_has_to_hold_exactly_the_strings_of_the_source(tmp_path):
    source = _source(tmp_path, 'def f():\n    return _("one"), _("two"), _("one")\n')
    assert catalog.compare(_pot(tmp_path, "one", "two"), source) == []
    assert any(
        "missing" in p and "'two'" in p for p in catalog.compare(_pot(tmp_path, "one"), source)
    )
    extra = catalog.compare(_pot(tmp_path, "one", "two", "three"), source)
    assert any("not in the source" in p and "'three'" in p for p in extra)


def test_the_header_entry_is_not_a_string(tmp_path):
    source = _source(tmp_path, "def f():\n    return 1\n")
    assert catalog.pot_msgids(_pot(tmp_path)) == set()
    assert catalog.compare(_pot(tmp_path), source) == []


def test_the_template_reader_joins_lines_and_unescapes(tmp_path):
    path = tmp_path / "x.pot"
    path.write_text(
        'msgid ""\nmsgstr ""\n\n'
        '#: a.py\n#, python-format\nmsgid ""\n"first line\\n"\n'
        '"second \\"quoted\\" %d"\nmsgstr ""\n\n'
        'msgid "plain"\nmsgstr "x"\n',
        encoding="utf-8",
    )
    assert catalog.pot_msgids(path) == {'first line\nsecond "quoted" %d', "plain"}


@pytest.mark.spawns_processes
def test_xgettext_finds_exactly_the_strings_the_reader_finds(tmp_path):
    if shutil.which("xgettext") is None:
        pytest.skip("xgettext is not on PATH (package gettext); the CI installs it before pytest")
    pot = tmp_path / "messages.pot"
    result = subprocess.run(
        ["bash", str(REPO / "tools" / "i18n.sh"), "extract", str(pot)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert catalog.compare(pot, PACKAGE) == []
    assert len(catalog.pot_msgids(pot)) > 70
