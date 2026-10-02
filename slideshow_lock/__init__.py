"""CI scaffolding for the slideshow-lock application.

This module intentionally contains no product logic yet. Its only purpose is
to give the GitHub Actions pipeline (ruff, pytest, xgettext) real source to
operate on before the application code (state machine, D-Bus layer, GTK
slideshow) lands from the other work items. Every string shown to a user
must go through ``_()`` so gettext can extract it (see D26).

``APP_ID`` is the single-source application identity constant (D17): the
`.desktop` file name, the GSettings schema id, the RPM package name, the
systemd unit name and the gettext domain are all meant to derive from this
one value. Nobody picks their own stand-in name; everything references
``APP_ID``.
"""

import gettext

_ = gettext.gettext

APP_ID = "io.github.trensoft.slideshowlock"


def app_display_name() -> str:
    """Return the translatable, user-facing application name placeholder."""
    return _("Slideshow Lock")
