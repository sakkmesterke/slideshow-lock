# SPDX-FileCopyrightText: 2026 TrenSoft
# SPDX-License-Identifier: GPL-3.0-or-later

"""Which language the interface speaks (gettext), and where the catalogs are looked for.

``slideshow_lock._`` is ``gettext.gettext``: it asks the *current* text domain at every call, so
``setup()`` only has to name the domain (``APP_ID``) and the directory that holds the compiled
catalogs. A catalog is ``<localedir>/<lang>/LC_MESSAGES/<APP_ID>.mo``; a new language is one
``po/<lang>.po`` (see ``tools/i18n.sh``), no code changes.

The language comes from the environment, in the order Python's gettext reads it: ``LANGUAGE``
(a ``:``-separated list), ``LC_ALL``, ``LC_MESSAGES``, ``LANG``; the first one that is not empty
wins, and ``C`` means no translation. A missing catalog is not an error: the interface stays
English. So does one that cannot be used (not a catalog, a charset Python does not know, bytes
that are not in the charset it declares): ``setup()`` logs one WARNING and the program starts.

The directory is ``$SLIDESHOW_LOCK_LOCALEDIR`` when that is set (``run.sh`` points it at the
catalogs it builds from a checkout), otherwise the system default of Python's gettext
(``<prefix>/share/locale``).
"""

from __future__ import annotations

import gettext
import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from slideshow_lock import APP_ID

LOCALEDIR_ENV = "SLIDESHOW_LOCK_LOCALEDIR"
LANGUAGE_ENV_VARS = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")

# A directory that holds no catalog on any system: <this>/<lang>/LC_MESSAGES/... does not exist.
_NO_CATALOG_DIR = os.devnull

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Status:
    """What ``setup()`` found: the language variables as they were, the directory, the catalogs."""

    environment: Tuple[Tuple[str, Optional[str]], ...]
    localedir: str
    catalogs: Tuple[str, ...]


def setup() -> Status:
    """Select the text domain and its directory. Call it first in ``main()``, before the command
    line is parsed, so that ``--help`` is translated too."""
    requested = os.environ.get(LOCALEDIR_ENV)
    if requested:
        gettext.bindtextdomain(APP_ID, os.path.abspath(requested))
    localedir = gettext.bindtextdomain(APP_ID)
    gettext.textdomain(APP_ID)
    catalogs = tuple(gettext.find(APP_ID, localedir, all=True))
    if catalogs and not _can_load(catalogs):
        # ``_()`` loads the catalogs lazily, at the first call, and Python's gettext swallows only
        # OSError there. Point the domain at a place with no catalog, so that no call ever
        # reaches the broken file.
        gettext.bindtextdomain(APP_ID, _NO_CATALOG_DIR)
        catalogs = ()
    return Status(
        environment=tuple((name, os.environ.get(name)) for name in LANGUAGE_ENV_VARS),
        localedir=localedir,
        catalogs=catalogs,
    )


def _can_load(catalogs: Tuple[str, ...]) -> bool:
    """Load the catalogs now, the way the first ``_()`` would (the result is cached by gettext,
    so that call finds it ready). ``False`` plus a WARNING when that fails."""
    try:
        gettext.translation(APP_ID, gettext.bindtextdomain(APP_ID))
    except Exception as error:  # struct.error, LookupError, UnicodeDecodeError, OSError, ...
        kind = type(error)
        name = (
            kind.__name__
            if kind.__module__ == "builtins"
            else "%s.%s" % (kind.__module__, kind.__name__)
        )
        _LOG.warning(
            "[config] the translation catalog %s cannot be used (%s: %s), "
            "the interface stays English",
            ",".join(catalogs),
            name,
            error,
        )
        return False
    return True


def log_status(status: Status) -> None:
    """One INFO line (``[config]``), so a log shows which language a process was given."""
    variables = " ".join(
        "%s=%s" % (name, value if value else "unset") for name, value in status.environment
    )
    _LOG.info(
        "[config] language %s, localedir %s, catalog %s",
        variables,
        status.localedir,
        ",".join(status.catalogs) if status.catalogs else "none (the interface stays English)",
    )
