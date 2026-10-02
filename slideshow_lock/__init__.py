"""CI scaffolding for the slideshow-lock application.

This module intentionally contains no product logic yet. Its only purpose is
to give the GitHub Actions pipeline (ruff, pytest, xgettext) real source to
operate on before the application code (state machine, D-Bus layer, GTK
slideshow) lands from the other work items. Every string shown to a user
must go through ``_()`` so gettext can extract it (see D26).
"""

import gettext

_ = gettext.gettext

APP_ID = "io.github.sakkmesterke.SlideshowLock"


def app_display_name() -> str:
    """Return the translatable, user-facing application name placeholder."""
    # D26 negative-control: temporarily un-wrapped on purpose to prove the
    # xgettext/.pot gate actually fails when a string is NOT marked for
    # translation. Restored to _(...) in the very next commit on this PR.
    return "Slideshow Lock"
