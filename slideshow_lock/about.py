# SPDX-FileCopyrightText: 2026 TrenSoft
# SPDX-License-Identifier: GPL-3.0-or-later

"""The one place that holds the donation link, and the check of it.

There is no About window: the main menu that opened it is gone since 1.0.10, and so are its code
and the strings of its link rows. What is left is ``DONATION_URL``, ``donation_link()``, which
decides whether an address may be shown, and ``DEVELOPER``.

The donation link is ``DONATION_URL``, the only place in the source that holds it. Anything that is
not a plain ``https://`` address (empty, blanks, the placeholder ``<DONATION_URL>``, another scheme,
spaces, a user name in the address) is not a link: the Donate button of the settings window is then
not shown, and nothing of it is visible. The README has a Support section only when the link is
valid, and with the same address (``tests/test_support_readme.py`` keeps the two together).

This module opens no connection and imports no GUI library.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

#: The donation link. The one place in the source that names it; the README "Support" section holds
#: the same address (``tests/test_support_readme.py``). Empty or invalid: the window shows no trace.
DONATION_URL = "https://www.paypal.com/donate/?hosted_button_id=QPJCYDA6UXDEJ"

#: The maker, as the line at the bottom right of the settings window shows it
#: ("<version> by TrenSoft", ``preferences.version_text``).
DEVELOPER = "TrenSoft"

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
