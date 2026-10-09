# SPDX-FileCopyrightText: 2026 TrenSoft
# SPDX-License-Identifier: GPL-3.0-or-later

"""The About window of the settings window, and the one place that holds the donation link.

Nothing opens it since 1.0.10: the main menu of the settings window is gone, and ``show_about`` is
not called (``docs/preferences.md``, "The About window"). When opened, it shows the name, the
version (``slideshow_lock.version``),
the short description, the sentence about the AI agents (the one of the README, "Authorship"),
the copyright, the licence, and links: the project page, the issue tracker and, when there is one,
the donation link.

Which class draws it depends on the libadwaita that is installed. ``Adw.AboutDialog`` (libadwaita
1.5 and later) is used when it exists, otherwise ``Adw.AboutWindow`` (1.2 and later). The window
class is deprecated from libadwaita 1.6 on, which is the libadwaita of EL10, so the program does
not call the deprecated class where the new one exists; the Requires of the package stays
``libadwaita >= 1.2``. Both are made with the same properties and ``add_link``; they differ in how
they are shown (a dialog on top of the parent, a transient window), which is all ``show_about``
decides (it has no caller since 1.0.10).

The donation link is ``DONATION_URL``, the only place in the source that holds it. Anything that is
not a plain ``https://`` address (empty, blanks, the placeholder ``<DONATION_URL>``, another scheme,
spaces, a user name in the address) is not a link: the window then has no donation row at all, and
nothing of it is visible. The README has a Support section only when the link is valid, and with the
same address (``tests/test_support_readme.py`` keeps the two together).

This module opens no connection: the links are opened by libadwaita when a row is activated.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gtk  # noqa: E402

from slideshow_lock import APP_ID, _, app_display_name  # noqa: E402
from slideshow_lock.version import program_version  # noqa: E402

#: The donation link. The one place in the source that names it; the README "Support" section holds
#: the same address (``tests/test_support_readme.py``). Empty or invalid: the window shows no trace.
DONATION_URL = "https://www.paypal.com/donate/?hosted_button_id=QPJCYDA6UXDEJ"

PROJECT_URL = "https://github.com/trensoft/slideshow-lock"
ISSUES_URL = PROJECT_URL + "/issues"

DEVELOPER = "TrenSoft"
#: The holder and year are those of the SPDX headers; not translated.
COPYRIGHT = "© 2026 TrenSoft"

_PRINTABLE = re.compile(r"[\x21-\x7e]+")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
_FORBIDDEN = frozenset('<>"\\{}|^`')


def donation_link(value: Optional[str] = None) -> Optional[str]:
    """*value* (default ``DONATION_URL``) if it is an address that may be shown, otherwise None.

    An address that may be shown is ``https://`` followed by a host name with a dot in it, made of
    printable ASCII without blanks and without the characters of a placeholder or of markup, and
    without a user name or password. There is no trimming: ``" https://a.b"`` is not one."""
    candidate = DONATION_URL if value is None else value
    if not isinstance(candidate, str) or not candidate.startswith("https://"):
        return None
    if not _PRINTABLE.fullmatch(candidate) or _FORBIDDEN & set(candidate):
        return None
    try:
        parts = urlsplit(candidate)
        parts.port  # a port that is not a number raises ValueError
    except ValueError:
        return None
    host = parts.hostname or ""
    if "@" in parts.netloc or "." not in host or not _HOST.fullmatch(host):
        return None
    return candidate


def links() -> List[Tuple[str, str]]:
    """The link rows, label and address, in the order shown: the project page, the issue tracker,
    and the donation link only when ``donation_link()`` accepts it."""
    rows = [(_("Project page"), PROJECT_URL), (_("Report an issue"), ISSUES_URL)]
    donation = donation_link()
    if donation is not None:
        rows.append((_("Support the project"), donation))
    return rows


def comments() -> str:
    """The description: what the program does, then the sentence about how it was written."""
    return (
        _("Fullscreen slideshow screensaver for GNOME. Any input after idle locks the session.")
        + "\n\n"
        + _("The code of this project was written with the help of AI agents.")
    )


def show_about(parent: Gtk.Window) -> None:
    """Open the About window over *parent*."""
    properties = dict(
        application_name=app_display_name(),
        application_icon=APP_ID,
        developer_name=DEVELOPER,
        version=program_version(),
        comments=comments(),
        copyright=COPYRIGHT,
        license_type=Gtk.License.GPL_3_0,
    )
    if hasattr(Adw, "AboutDialog"):
        about = Adw.AboutDialog(**properties)
        _add_links(about)
        about.present(parent)
        return
    about = Adw.AboutWindow(transient_for=parent, modal=True, **properties)
    _add_links(about)
    about.present()


def _add_links(about) -> None:
    for label, address in links():
        about.add_link(label, address)
