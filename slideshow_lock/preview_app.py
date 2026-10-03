"""Run the slideshow preview from a terminal (CORE-2).

    glib-compile-schemas data/
    GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.preview_app [--folder PATH]

One fullscreen window per monitor, the pictures of the folder, any key press, mouse
movement, click or scroll ends it. It never locks the session (D11) and does not touch
the stored settings: ``--interval``, ``--order``, ``--scaling`` and ``--pan`` only apply
to this run. Without ``--folder`` and the other options the stored settings are used; the
stored folder, if none was chosen, is the system's pictures folder itself, read
recursively (``~/Képek`` on a Hungarian system, ``~/Pictures`` if none is configured).

``start_preview`` is the part the settings window (UI-1) and the service (CORE-1) will
call: it builds the controller from the real GTK windows, the real scaler and the GLib
clock and worker. ``build_source`` builds the source for it (with the loader probe).

``Gtk.Application`` registers itself on the session bus, which opens one session-bus
connection (measured). That is application registration, not a lock call: nothing here
calls the session, screensaver or login manager (the test in ``tests/test_preview.py``
scans for exactly that).
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Callable, List, Optional

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

from slideshow_lock import APP_ID, _  # noqa: E402
from slideshow_lock.image_source import ImageSource, source_from_settings  # noqa: E402
from slideshow_lock.preview import GLibClock, PreviewController, ThreadWorker  # noqa: E402
from slideshow_lock.preview_window import animations_enabled, open_monitor_windows  # noqa: E402
from slideshow_lock.scaling import ImageScaler, probe_loadable  # noqa: E402
from slideshow_lock.settings import (  # noqa: E402
    KEY_ORDER,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_PICTURE_FOLDER,
    KEY_SCALING,
    KEY_SLIDE_INTERVAL_SECONDS,
    Settings,
)

_LOG = logging.getLogger(__name__)


class SessionSettings:
    """The stored settings, with some values replaced for this run only (never written)."""

    def __init__(self, settings: Settings, overrides: dict) -> None:
        self._settings = settings
        self._overrides = overrides

    def connect_changed(self, callback: Callable[[str], None]) -> int:
        return self._settings.connect_changed(callback)

    def _get(self, key: str, stored):
        return self._overrides[key] if key in self._overrides else stored()

    def get_picture_folder(self) -> str:
        return self._get(KEY_PICTURE_FOLDER, self._settings.get_picture_folder)

    def get_order(self) -> str:
        return self._get(KEY_ORDER, self._settings.get_order)

    def get_scaling(self) -> str:
        return self._get(KEY_SCALING, self._settings.get_scaling)

    def get_slide_interval_seconds(self) -> int:
        return self._get(KEY_SLIDE_INTERVAL_SECONDS, self._settings.get_slide_interval_seconds)

    def get_pan_portrait_images(self) -> bool:
        return self._get(KEY_PAN_PORTRAIT_IMAGES, self._settings.get_pan_portrait_images)


def build_source(settings) -> ImageSource:
    """The image source for a preview: not started, and with the loader probe in place, so a
    picture that no installed gdk-pixbuf loader can read never gets into the queue."""
    return source_from_settings(settings, probe=probe_loadable)


def start_preview(settings, source: ImageSource) -> PreviewController:
    """Open the preview windows and start showing. *source* must be started by the caller.

    The returned controller is for this one preview: its worker thread is closed when it stops.
    """
    worker = ThreadWorker()
    controller = PreviewController(
        source,
        settings,
        open_monitor_windows,
        ImageScaler(),
        clock=GLibClock(),
        worker=worker,
        animations=animations_enabled,
    )
    controller.connect_stopped(lambda _reason: worker.close())  # do not leave a thread behind
    controller.start()
    return controller


def _parse(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m slideshow_lock.preview_app",
        description=_("Show the slideshow preview. Any input ends it. It never locks the session."),
    )
    parser.add_argument("--folder", help=_("picture folder (this run only)"))
    parser.add_argument("--interval", type=int, help=_("seconds per picture (this run only)"))
    parser.add_argument("--order", choices=("random", "name"), help=_("picture order"))
    parser.add_argument("--scaling", choices=("fit", "fill"), help=_("fit or fill the monitor"))
    parser.add_argument(
        "--pan", action="store_true", help=_("scroll portrait pictures slowly (fill mode)")
    )
    parser.add_argument("--debug", action="store_true", help=_("log every step"))
    return parser.parse_args(argv)


def overrides_from_args(args: argparse.Namespace) -> dict:
    """The settings this run replaces, from the command line. Raises ``ValueError`` (with the
    message to print) for a value outside its range. Nothing is replaced unless it was given."""
    overrides = {}
    if args.folder is not None:
        overrides[KEY_PICTURE_FOLDER] = args.folder
    if args.interval is not None:
        if not 1 <= args.interval <= 3600:
            raise ValueError(_("The interval must be between 1 and 3600 seconds."))
        overrides[KEY_SLIDE_INTERVAL_SECONDS] = args.interval
    if args.order is not None:
        overrides[KEY_ORDER] = args.order
    if args.scaling is not None:
        overrides[KEY_SCALING] = args.scaling
    if args.pan:
        overrides[KEY_PAN_PORTRAIT_IMAGES] = True
    return overrides


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    schema = Gio.SettingsSchemaSource.get_default()
    if schema is None or schema.lookup(APP_ID, True) is None:
        print(
            _(
                "The settings schema is not installed. Compile it and point GSettings at it:\n"
                "  glib-compile-schemas data/\n"
                "  GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.preview_app"
            ),
            file=sys.stderr,
        )
        return 2

    try:
        overrides = overrides_from_args(args)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    settings = SessionSettings(Settings(), overrides)

    app = Gtk.Application(application_id=APP_ID + ".Preview")
    state = {"controller": None, "source": None}

    def on_activate(application: Gtk.Application) -> None:
        application.hold()
        source = build_source(settings)
        source.start()
        controller = start_preview(settings, source)
        state["source"], state["controller"] = source, controller
        controller.connect_stopped(lambda _reason: GLib.idle_add(application.quit))
        if not controller.running:  # no monitor
            application.quit()

    app.connect("activate", on_activate)
    status = app.run([sys.argv[0]])
    if state["source"] is not None:
        state["source"].stop()
    return status


if __name__ == "__main__":
    sys.exit(main())
