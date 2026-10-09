# SPDX-FileCopyrightText: 2026 TrenSoft
# SPDX-License-Identifier: GPL-3.0-or-later

"""The settings window as the program starts it: ``slideshow-lock settings``.

The window and the preview code may not name the lock, the session or the bus (D11,
``tests/test_preview.py``). This module is on the lock side of that line, so it is the one that
gives the window's Preview button what needs the bus: before the preview opens its windows, the
shell's overview (Super) is closed, as it is before the slideshow of the idle path
(``GnomeShellOverview``, ``docs/architecture/dbus-state-machine.md``, section 3.7a). The window
only gets a callback and does not know what it does.

``python3 -m slideshow_lock.preferences`` still starts the window without that step.
"""

from __future__ import annotations

import logging
import sys
from typing import List, Optional

from slideshow_lock import dbus_adapters
from slideshow_lock.preferences import main as preferences_main

_LOG = logging.getLogger(__name__)


def close_overview() -> None:
    """Close the shell's overview if it is open. Never raises: whatever fails is logged and the
    preview starts as it would have (``GnomeShellOverview`` does not raise, the bus is the rest).
    A new adapter is made at each press: a failure logs one warning per press, not per process."""
    try:
        overview = dbus_adapters.GnomeShellOverview(dbus_adapters.session_bus())
    except Exception as exc:
        _LOG.warning("[slideshow] the overview is not closed before the preview (%s)", exc)
        return
    overview.close_if_open()


def main(argv: Optional[List[str]] = None) -> int:
    return preferences_main(argv, before_preview=close_overview)


if __name__ == "__main__":
    sys.exit(main())
