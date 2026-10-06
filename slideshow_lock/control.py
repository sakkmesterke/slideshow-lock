"""The command ``slideshowlock``: start the background service, then open the settings window.

    slideshowlock              from the menu or a terminal: start the service, open the window
    slideshowlock autostart    at login (the XDG autostart entry): start the service; open the
                               window only the first time, when no picture folder has been chosen

The service is the systemd user unit ``slideshow-lock.service``. It is started through the user's
systemd on the session bus (``ResetFailedUnit``, then ``StartUnit``), which leaves a unit that is
already running alone, so the command can be run any number of times, and the unit stays the
one process that is supervised and logged under its own name. Nothing is enabled and no preset is
installed: the unit is not started at login by systemd, the autostart entry of the package starts
it through this command.

The window and the preview code may not name the session or the bus (D11,
``tests/test_preview.py``), so the start of the service is here, on the lock side of that line, and
the window is the one of ``settings_app`` (``slideshow-lock settings``), called after it in the same
process.

A service that cannot be started is a warning and the window opens all the same: a settings window
that does not appear because of the bus is worse than one that appears next to a service that is
not running. The exit status is the window's; without a window (the login start with nothing to
show) it is 1 when the service could not be started and 0 otherwise.

The first start. The login start opens the window while ``first-run-done`` is false and the user has
stored no picture folder (``Gio.Settings.get_user_value``, so the default does not count), and sets
the key before it opens the window: the window appears once, also when it is closed without a
folder. ``slideshowlock`` without ``autostart`` neither reads nor sets the key.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

import gi

gi.require_version("Gio", "2.0")

from gi.repository import Gio  # noqa: E402

from slideshow_lock import APP_ID, _, dbus_adapters, i18n, settings_app  # noqa: E402
from slideshow_lock.settings import Settings  # noqa: E402

_LOG = logging.getLogger(__name__)

#: The unit the package installs (``data/slideshow-lock.service``).
SERVICE_UNIT = "slideshow-lock.service"

AUTOSTART = "autostart"


def _one_line(text: str) -> str:
    return text.replace("\n", " ")[:200]


def _reason(exc: Exception) -> str:
    """What a failed D-Bus call says, on one line (``GLib.Error`` keeps it in ``message``)."""
    return _one_line(getattr(exc, "message", None) or str(exc))


def start_service() -> bool:
    """Ask the user's systemd to start the service unit. ``True`` when it accepted the start job.

    ``ResetFailedUnit`` goes first: a unit that ran out of its start limit stays ``failed``, and
    a failed unit is not started again. Whatever it answers, ``StartUnit`` is tried (it answers
    with an error for a unit that is not loaded, which is the case before the first start, and
    that is not a reason to skip the start). Never raises: a failure is one WARNING."""
    try:
        manager = dbus_adapters.SystemdUserManager(dbus_adapters.session_bus())
    except Exception as exc:
        _LOG.warning("[slideshow] the service is not started: no session bus (%s)", _reason(exc))
        return False
    try:
        manager.reset_failed(SERVICE_UNIT)
    except Exception as exc:
        _LOG.debug("[slideshow] ResetFailedUnit(%s) answered: %s", SERVICE_UNIT, _reason(exc))
    try:
        job = manager.start(SERVICE_UNIT)
    except Exception as exc:
        _LOG.warning(
            "[slideshow] the service is not started: systemd refused StartUnit(%s) (%s)",
            SERVICE_UNIT,
            _reason(exc),
        )
        return False
    _LOG.info("[slideshow] start of %s requested (job %s)", SERVICE_UNIT, job)
    return True


def first_run_window_wanted() -> bool:
    """Is this the first login, with no picture folder chosen? Sets ``first-run-done`` when it is
    (the window is about to open), so the answer is yes once. Never raises."""
    schema = Gio.SettingsSchemaSource.get_default()
    if schema is None or schema.lookup(APP_ID, True) is None:
        # the window says so itself (exit status 2); at login there is nothing to open it for
        _LOG.warning("[config] the settings schema is not installed: no first-run window")
        return False
    settings = Settings()
    if settings.get_first_run_done() or settings.has_chosen_picture_folder():
        return False
    if not settings.set_first_run_done(True):
        _LOG.warning("[config] first-run-done cannot be stored: the window opens at every login")
    return True


def _parse(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="slideshowlock",
        description=_("Start the slideshow service and open the settings window."),
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=(AUTOSTART,),
        help=_("at login: start the service, open the window only the first time"),
    )
    parser.add_argument("--debug", action="store_true", help=_("log every step"))
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    i18n.setup()  # before the command line is parsed: --help is translated too
    args = _parse(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    started = start_service()
    if args.mode == AUTOSTART and not first_run_window_wanted():
        return 0 if started else 1
    return settings_app.main(["--debug"] if args.debug else [])


if __name__ == "__main__":
    sys.exit(main())
