"""The OpenGL renderer string, read from a GL context through ``glGetString``.

The display, the context and the libraries are fakes; what is checked is the order of the calls
(a context made, realized and made current before the string is asked for, cleared afterwards), the
libraries that are tried and that nothing but their sonames is loaded, and that every failure is
None. That the real thing answers on a real display was measured with ``tools/wayland-smoke``'s
headless compositor, not here.
"""

from __future__ import annotations

import pytest

from slideshow_lock import gl_probe
from slideshow_lock.gl_probe import GL_RENDERER, LIBRARY_GL, LIBRARY_GLES, read_gl_renderer


class _Context:
    log: list = []

    def __init__(self, log, use_es=False, fail_at=None):
        self.log = log
        self._use_es = use_es
        self._fail_at = fail_at

    def _step(self, name):
        self.log.append(name)
        if self._fail_at == name:
            raise RuntimeError(name + " failed")

    def realize(self):
        self._step("realize")

    def make_current(self):
        self._step("make_current")

    def get_use_es(self):
        return self._use_es


def _context_class(log, **kwargs):
    class Context(_Context):
        @staticmethod
        def clear_current():
            log.append("clear_current")

    return Context(log, **kwargs)


class _Display:
    def __init__(self, context=None, error=None):
        self._context = context
        self._error = error

    def create_gl_context(self):
        if self._error is not None:
            raise self._error
        self._context.log.append("create")
        return self._context


class _Function:
    def __init__(self, answer, log):
        self.answer, self.log = answer, log
        self.restype = self.argtypes = None

    def __call__(self, name):
        self.log.append(("glGetString", name))
        return self.answer


class _Library:
    def __init__(self, answer, log, name):
        self.glGetString = _Function(answer, log)
        self.name = name


def loader_for(log, answers):
    """A loader whose libraries answer *answers* (library name -> bytes/None, or an exception)."""

    def load(name):
        log.append(("load", name))
        answer = answers.get(name, OSError(name + ": cannot open shared object file"))
        if isinstance(answer, Exception):
            raise answer
        return _Library(answer, log, name)

    return load


def test_the_string_is_read_from_a_current_context_and_the_context_is_cleared_after():
    log = []
    display = _Display(_context_class(log))
    loader = loader_for(log, {LIBRARY_GL: b"Mesa Intel(R) UHD Graphics 620 (KBL GT2)"})
    assert read_gl_renderer(display, loader) == "Mesa Intel(R) UHD Graphics 620 (KBL GT2)"
    assert log == [
        "create",
        "realize",
        "make_current",
        ("load", LIBRARY_GL),
        ("glGetString", GL_RENDERER),
        "clear_current",
    ]


def test_the_renderer_name_is_the_gl_renderer_constant_not_the_vendor_or_version():
    assert GL_RENDERER == 0x1F01  # GL_RENDERER; GL_VENDOR is 0x1F00 and GL_VERSION 0x1F02


def test_a_gles_context_looks_in_the_gles_library_first():
    log = []
    display = _Display(_context_class(log, use_es=True))
    loader = loader_for(log, {LIBRARY_GL: b"from GL", LIBRARY_GLES: b"from GLES"})
    assert read_gl_renderer(display, loader) == "from GLES"
    assert ("load", LIBRARY_GL) not in log


def test_the_other_library_is_tried_when_the_first_is_missing_or_has_no_answer():
    log = []
    display = _Display(_context_class(log))
    loader = loader_for(log, {LIBRARY_GLES: b"from GLES"})  # libGL cannot be opened
    assert read_gl_renderer(display, loader) == "from GLES"
    log2 = []
    loader2 = loader_for(log2, {LIBRARY_GL: None, LIBRARY_GLES: b"from GLES"})  # NULL answer
    assert read_gl_renderer(_Display(_context_class(log2)), loader2) == "from GLES"


def test_no_library_is_none_and_the_context_is_still_cleared():
    log = []
    assert read_gl_renderer(_Display(_context_class(log)), loader_for(log, {})) is None
    assert log[-1] == "clear_current"


@pytest.mark.parametrize("answer", [None, b"", b"  \n"])
def test_an_empty_answer_is_none(answer):
    log = []
    loader = loader_for(log, {LIBRARY_GL: answer, LIBRARY_GLES: answer})
    assert read_gl_renderer(_Display(_context_class(log)), loader) is None


def test_bytes_that_are_not_utf8_do_not_raise():
    log = []
    loader = loader_for(log, {LIBRARY_GL: b"GPU \xff\xfe"})
    text = read_gl_renderer(_Display(_context_class(log)), loader)
    assert text is not None and text.startswith("GPU ")


@pytest.mark.parametrize("step", ["realize", "make_current"])
def test_a_context_that_cannot_be_made_current_is_none_and_no_library_is_touched(step):
    log = []
    display = _Display(_context_class(log, fail_at=step))
    assert read_gl_renderer(display, loader_for(log, {LIBRARY_GL: b"x"})) is None
    assert not [entry for entry in log if isinstance(entry, tuple)]
    assert log[-1] == "clear_current"


def test_a_display_without_gl_is_none():
    log = []
    display = _Display(error=RuntimeError("OpenGL is not available"))
    assert read_gl_renderer(display, loader_for(log, {LIBRARY_GL: b"x"})) is None
    assert log == []  # nothing loaded, nothing to clear


def test_a_context_that_cannot_be_cleared_does_not_change_the_answer():
    log = []

    class Stubborn(_Context):
        @staticmethod
        def clear_current():
            raise RuntimeError("cannot clear")

    display = _Display(Stubborn(log))
    assert read_gl_renderer(display, loader_for(log, {LIBRARY_GL: b"Mesa X"})) == "Mesa X"


def test_only_the_two_dispatch_libraries_are_ever_loaded_and_by_soname():
    assert (LIBRARY_GL, LIBRARY_GLES) == ("libGL.so.1", "libGLESv2.so.2")
    log = []
    read_gl_renderer(_Display(_context_class(log)), loader_for(log, {}))
    loaded = [entry[1] for entry in log if isinstance(entry, tuple) and entry[0] == "load"]
    assert loaded == [LIBRARY_GL, LIBRARY_GLES]
    assert all("/" not in name for name in loaded)


def test_the_function_is_declared_to_return_a_c_string_taking_an_unsigned_int():
    import ctypes

    log = []
    library = _Library(b"x", log, "lib")
    assert gl_probe._get_string(library) == "x"
    assert library.glGetString.restype is ctypes.c_char_p
    assert library.glGetString.argtypes == [ctypes.c_uint]
