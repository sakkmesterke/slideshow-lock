"""The OpenGL renderer string of the display GTK draws on: what the driver calls itself.

GTK and PyGObject do not give it: ``Gsk.Renderer`` only has its type name, ``Gdk.GLContext`` the
version and the API. A GL context does answer ``glGetString(GL_RENDERER)`` once it is current, and
that is the one place where Mesa says ``llvmpipe`` when it has fallen back to the CPU by itself
(which GTK reports as an ordinary GL renderer), so it is read here through ``ctypes``:

* a context is made with ``Gdk.Display.create_gl_context()`` (GTK's own, on the display GTK draws
  on), realized and made current for the moment of the call, and cleared again;
* ``glGetString`` is looked up in ``libGL.so.1`` and ``libGLESv2.so.2``, the GLVND dispatch
  libraries (GTK's own OpenGL loader reaches the driver through the same ones), by their sonames
  only, which the dynamic loader resolves; nothing else is loaded, started or written.

Any failure (no GL, no library, no string) is None, which ``slideshow_lock.effects`` takes for a
machine that is not known to have a GPU. The GDK calls need GDK's main thread, so call this from it.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Any, Callable, Optional

_LOG = logging.getLogger(__name__)

GL_RENDERER = 0x1F01  # glGetString's name for the renderer string

#: The libraries ``glGetString`` is looked up in, the one that fits the context's API first.
LIBRARY_GL = "libGL.so.1"
LIBRARY_GLES = "libGLESv2.so.2"


def _get_string(library: Any) -> Optional[str]:
    """``glGetString(GL_RENDERER)`` of the loaded *library*; None if it has none or answers NULL."""
    function = library.glGetString
    function.restype = ctypes.c_char_p
    function.argtypes = [ctypes.c_uint]
    text = function(GL_RENDERER)
    if not text:
        return None
    return text.decode("utf-8", "replace").strip() or None


def read_gl_renderer(display: Any, loader: Callable[[str], Any] = ctypes.CDLL) -> Optional[str]:
    """The OpenGL renderer string of *display* (a ``Gdk.Display``), None if it cannot be read.
    *loader* turns a library name into a loaded library (``ctypes.CDLL``; a test gives its own)."""
    context = None
    try:
        context = display.create_gl_context()
        context.realize()
        context.make_current()
        names = (LIBRARY_GLES, LIBRARY_GL) if context.get_use_es() else (LIBRARY_GL, LIBRARY_GLES)
        for name in names:
            try:
                text = _get_string(loader(name))
            except (OSError, AttributeError) as error:
                _LOG.debug("[gl-probe] %s: %s", name, error)
                continue
            if text:
                return text
        return None
    except Exception as error:  # noqa: BLE001 - GLib.Error (no GL), a missing method: all "unknown"
        _LOG.debug("[gl-probe] no OpenGL renderer string: %s", error)
        return None
    finally:
        if context is not None:
            try:
                type(context).clear_current()
            except Exception as error:  # noqa: BLE001
                _LOG.debug("[gl-probe] could not clear the context: %s", error)
