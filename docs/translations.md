# Translations

The interface follows the language of the session (gettext). The text in the source is English:
every user-facing string goes through `_()` (`slideshow_lock/__init__.py`), and without a catalog
the interface is exactly that English text. A missing catalog is never an error. A catalog that cannot
be used (not a catalog, a charset Python does not know, bytes that are not in the charset it declares)
is not an error either: the interface is English and the program writes one WARNING line (section 1).

Status: there are five catalogs: German, Spanish, French, Hungarian and Italian (`po/de.po`, `po/es.po`,
`po/fr.po`, `po/hu.po`, `po/it.po`, all listed in `po/LINGUAS`). Each holds a translation of every string
the source asks for (`tools/i18n.sh check` compares the template with the source; the tests compare
each catalog with the source). A native speaker of these languages has not read them yet. The
`usage:` and `options:` lines of `--help` come from Python's `argparse` and stay English (section 5).

## 1. How the language is chosen

`slideshow_lock/i18n.py`, `setup()`, called first in `main()` of the preview, the settings window
and the service (before the command line is parsed, so `--help` is translated too):

- **Language.** The environment, in the order Python's gettext reads it: `LANGUAGE` (a `:`
  separated list, `de:hu` falls back from German to Hungarian), `LC_ALL`, `LC_MESSAGES`, `LANG`.
  The first one that is not empty wins; `C` means no translation. `hu_HU.UTF-8` finds `hu_HU`, then
  `hu`.
- **Catalogs.** `<localedir>/<lang>/LC_MESSAGES/<APP_ID>.mo`. The text domain is `APP_ID`
  (`io.github.trensoft.slideshowlock`).
- **Directory.** `$SLIDESHOW_LOCK_LOCALEDIR` when it is set (`run.sh` sets it), otherwise the default
  of Python's gettext, `<prefix>/share/locale`.

Each of the three programs logs one line at start, so a log shows which language it was given:

```
INFO [config] language LANGUAGE=hu LC_ALL=unset LC_MESSAGES=unset LANG=hu_HU.UTF-8, localedir /usr/share/locale, catalog /usr/share/locale/hu/LC_MESSAGES/io.github.trensoft.slideshowlock.mo
INFO [config] language LANGUAGE=unset LC_ALL=unset LC_MESSAGES=unset LANG=C.UTF-8, localedir /usr/share/locale, catalog none (the interface stays English)
```

What a process gets as `LANG` depends on how it was started (a terminal, the desktop session, a
systemd user unit); that is what this line is for.

A catalog that cannot be used is found by `setup()`, which loads it right away (Python's gettext
would do it at the first `_()`, and stops the program there for everything but `OSError`). The
WARNING names the file and the error, the line above says `catalog none`, and the interface is
English. The WARNING is one line on stderr with the message only, no time stamp and no level:
`setup()` runs before the command line is parsed (so that `--help` is translated), and so before
`logging.basicConfig` gives the log its format. The `[config]` line that follows has the usual
format. The catalogs of a `LANGUAGE` list are loaded together, so one broken catalog (`de:hu`)
makes the whole interface English.

## 3. Adding a language

1. `tools/i18n.sh update` writes the template `po/messages.pot` (not in git) and merges it into the
   catalogs that exist. For a new language start from the template:
   `msginit --no-translator -l <lang> -i po/messages.pot -o po/<lang>.po`, then set the header to
   `charset=UTF-8` (msginit writes the charset of the shell's locale, `ASCII` under `C`).
2. Translate `po/<lang>.po`, and add `<lang>` to `po/LINGUAS`.
3. `tools/i18n.sh check` (the CI runs it): `po/LINGUAS` and the `.po` files agree, every catalog
   passes `msgfmt -c --check-format`, the header entry has the line
   `"Content-Type: text/plain; charset=UTF-8\n"` on its own (`UTF-8bogus` does not count, nor does
   a line of a later entry), the name is a plain name (letters, digits, `_`, `@`, `.`; no slash, no
   leading dot, no hyphen), and the template holds exactly the strings the source asks for. No
   hyphen because a session says `pt_BR.UTF-8`, and Python's gettext then looks for the
   directories `pt_BR` and `pt`, not for `pt-BR`: the catalog of Brazilian Portuguese is `pt_BR.po`.

No code changes. `tools/i18n.sh build DIR` compiles the catalogs of `po/LINGUAS` into
`DIR/<lang>/LC_MESSAGES/<APP_ID>.mo`; `.mo` and `.pot` files are not in git. `build` runs the name
and charset checks first, and a build that fails leaves no `.mo` behind.

## 4. Rules for the source

`tools/i18n.sh check` and `tests/test_i18n_source.py` enforce them:

- `_()` takes **one plain string literal**: no variable, no f-string, no concatenation. xgettext
  cannot see anything else. Fill in values with `%` or `format` *after* the call, on the translated
  text.
- `_()` runs **inside a function**. A call in a module body, a class body or a default argument runs
  at import, before `setup()` has chosen the language, and would stay English.
- Plural forms (`ngettext`) are not used yet.

## 5. Not translated

- The log (English, `docs/logging-and-lifecycle.md`).
- The settings schema `summary` and `description` (they are references for people who read the
  schema, not interface text), so the schema carries no `gettext-domain`.
- Strings that Python's own `argparse` shows (`usage:`, `options:`): they come from Python's
  gettext, which now asks this domain, so they are translated only if a catalog contains them.

## 6. From a checkout

`./run.sh preview|settings|service` compiles the catalogs of `po/LINGUAS` into
`${XDG_CACHE_HOME:-$HOME/.cache}/slideshow-lock/locale` (outside the checkout, rebuilt at every
start) and points the program there. `locale` is a link to a directory that is built apart and not
changed afterwards, so two `run.sh` started together do not delete each other's catalogs; the
replaced directories are deleted a minute later. While `po/` has no `.po` file, nothing is built
and `SLIDESHOW_LOCK_LOCALEDIR` is not set. Without `msgfmt` (package `gettext`) `run.sh` warns once
and the interface stays English; the same for a catalog that does not compile.

To see a translated interface from a checkout, start it in a session language of that language, for
example `LANG=hu_HU.UTF-8 ./run.sh settings` (or `de_DE`, `es_ES`, `fr_FR`, `it_IT`).
