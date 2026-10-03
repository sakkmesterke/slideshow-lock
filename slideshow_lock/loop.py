"""Posting a function to a GLib main context, from any thread (CORE-1).

The service has two loops: the main one (GTK, the idle path, the slideshow) and the sleep
guard's own (``sleep_guard.GuardThread``). ``current_poster()`` captures the loop of the
calling thread, so a callback can later be handed back to it from another thread without
waiting: a post only queues the function, it never runs it in the caller, and it does not
block if the target loop is busy or stuck.
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib  # noqa: E402

Poster = Callable[[Callable[[], None]], None]


def poster_for(context: "GLib.MainContext") -> Poster:
    """A function that runs what it is given once, later, on *context*."""

    def post(fn: Callable[[], None]) -> None:
        def run(*_user_data) -> bool:
            fn()
            return GLib.SOURCE_REMOVE

        source = GLib.idle_source_new()
        source.set_callback(run)
        source.attach(context)

    return post


def current_poster() -> Poster:
    """``poster_for`` the loop that runs the calling thread (its thread-default context, else
    the global default one)."""
    context = GLib.MainContext.ref_thread_default()
    if context is None:
        context = GLib.MainContext.default()
    return poster_for(context)
