# Translations

The interface follows the language of the session (gettext). The text in the source is English:
every user-facing string goes through `_()` (`slideshow_lock/__init__.py`), and without a catalog
the interface is exactly that English text. A missing catalog is never an error. A catalog that cannot
be used (not a catalog, a charset Python does not know, bytes that are not in the charset it declares)
is not an error either: the interface is English and the program writes one WARNING line (section 1).

Status: there are 40 catalogs, all listed in `po/LINGUAS`: German, Spanish, French, Hungarian and
Italian (the first five, 1.0.0), and 35 more added in 1.0.1: Bulgarian, Catalan, Czech, Danish, Greek,
Estonian, Finnish, Irish, Croatian, Lithuanian, Latvian, Maltese, Dutch, Polish, Portuguese
(`pt`), Brazilian Portuguese (`pt_BR`), Romanian, Slovak, Slovenian, Serbian (Cyrillic), Swedish,
Norwegian Bokmål (`nb`), Ukrainian, Russian, Japanese, Simplified Chinese (`zh_CN`), Traditional
Chinese (`zh_TW`), Arabic, Hebrew, Hindi, Filipino (`fil`), Indonesian, Thai, Vietnamese and Korean.
Each catalog holds a translation of every string the source asks for (`tools/i18n.sh check`
compares the template with the source; the tests compare each catalog with the source).

**Who translated them.** The 35 catalogs of 1.0.1 were translated with AI assistance, and no native
speaker has reviewed them: the header of each says `Last-Translator: AI-assisted (Claude), not
reviewed by a native speaker`. A native speaker of the five catalogs of 1.0.0 has not read them
yet either. The tests check that a catalog is whole, consistent with its glossary and wired in, not
that the language is good (section 7). Corrections are welcome: open an issue, or a pull request on
`po/<lang>.po` (and on `docs/translation-glossary/<lang>.tsv` when it is a word the glossary holds).

The `usage:` and `options:` lines of `--help` come from Python's `argparse` and stay English
(section 5).

## 1. How the language is chosen

`slideshow_lock/i18n.py`, `setup()`, called first in `main()` of the preview, the settings window
and the service (before the command line is parsed, so `--help` is translated too):

- **Language.** The environment, in the order Python's gettext reads it: `LANGUAGE` (a `:`
  separated list, `de:hu` falls back from German to Hungarian), `LC_ALL`, `LC_MESSAGES`, `LANG`.
  The first one that is not empty wins; `C` means no translation. `hu_HU.UTF-8` finds `hu_HU`, then
  `hu`.
- **Catalogs.** `<localedir>/<lang>/LC_MESSAGES/<APP_ID>.mo`. The text domain is `APP_ID`
  (`io.github.sakkmesterke.SlideshowLock`).
- **Directory.** `$SLIDESHOW_LOCK_LOCALEDIR` when it is set (`run.sh` sets it), otherwise the default
  of Python's gettext, `<prefix>/share/locale`.

Each of the three programs logs one line at start, so a log shows which language it was given:

```
INFO [config] language LANGUAGE=hu LC_ALL=unset LC_MESSAGES=unset LANG=hu_HU.UTF-8, localedir /usr/share/locale, catalog /usr/share/locale/hu/LC_MESSAGES/io.github.sakkmesterke.SlideshowLock.mo
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

A new language also needs its header (`Language:` is the name of the file, `Plural-Forms:` is the true
rule of the language, `Last-Translator:` says who made the translation) and its glossary
(`docs/translation-glossary/<lang>.tsv`, section 7). No code changes. `tools/i18n.sh build DIR` compiles the catalogs of `po/LINGUAS` into
`DIR/<lang>/LC_MESSAGES/<APP_ID>.mo`; `.mo` and `.pot` files are not in git. `build` runs the name
and charset checks first, and a build that fails leaves no `.mo` behind.

The launcher and the AppStream metadata go through the same catalogs. `data/<APP_ID>.desktop.in`
and `data/<APP_ID>.metainfo.xml.in` are templates, and the only copies in git: `tools/i18n.sh data
DIR` writes `DIR/<APP_ID>.desktop` and `DIR/<APP_ID>.metainfo.xml` with the translations in them
(`msgfmt --desktop` and `msgfmt --xml`; the second needs the ITS rules that come with gettext).
`extract` adds their strings to the template: the `Name` and `Comment` of the launcher, and the name,
summary and description of the metadata (each paragraph is one string). Nothing else is translated
there, the `Keywords` line included. The five strings of the launcher and the metadata (the
comment of the launcher, the summary and the three paragraphs of the description) are in the 35
catalogs of 1.0.1; the five of 1.0.0 do not hold them yet, so for German, Spanish, French,
Hungarian and Italian the generated files are English until they do (`WITHOUT_DATA_STRINGS` in
`tests/i18n_catalogs.py` lists them). `check` also builds the two files.

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

## 7. Header, plural rule and glossary

`tests/test_i18n_catalogs.py` and `tests/test_i18n_translations.py` read the real catalogs, with
nothing started but `msgfmt` (the parts that need it skip without it, and the CI installs it).

- **Header.** `Language:` is the name of the file (`pt_BR.po` says `pt_BR`), `Content-Type` says
  UTF-8, `Last-Translator:` is `Slideshow Lock contributors` for the five of 1.0.0 and `AI-assisted
  (Claude), not reviewed by a native speaker` for the others. No entry is fuzzy, obsolete or empty.
- **`Plural-Forms:`** is the true rule of the language even though the source does not use
  `ngettext` yet. The test reads the expression with a small parser (never `eval`), evaluates it for
  n = 0..200, and fails when a result is outside `0..nplurals-1` or a form is never chosen. For the
  languages that the GNU gettext manual (node "Plural forms") and `msginit` of gettext 0.21 know,
  the rule must also give the same form for n = 0..1000 as the table (`REFERENCE_PLURALS` in
  `tests/test_i18n_catalogs.py`). Not in that table, so only checked to be evaluable: Catalan,
  Filipino, Hindi, Indonesian, Maltese, Thai, Simplified and Traditional Chinese. Irish and Hebrew
  follow the manual (three forms, two forms); Unicode CLDR has more forms for both, which matters the
  day the program uses `ngettext`.
- **Glossary.** A translation is only as consistent as its words. For each of the 35 new
  languages `docs/translation-glossary/<lang>.tsv` names the one translation the catalog uses for
  the key concepts: `slideshow`, `lock`, `preview`, `settings`, `idle`, `folder`, `picture`,
  `fullscreen`, `monitor` and `product` (the name, `Slideshow Lock`, which stays as it is). The file
  is UTF-8, tab separated, one concept per line: concept, translation, and an optional third column
  (a regular expression for the English words, read only for a concept that is not one of the ten).
  A language that inflects lists the stems or forms it needs, separated by `|`: `obraz` for
  `obrazy`, `obrazów`, `obrazu`. Blank lines and lines starting with `#` are skipped.
  **The rule:** a message is *about* a concept when the English words of the concept are in its
  msgid (after the product name and the indented command lines are taken out); the msgstr of such a
  message has to hold at least one of the glossary forms of the concept, compared without regard to
  upper and lower case. A message that is not about a concept is not looked at. So a catalog that
  translates "folder" two ways, or drops the word, fails the test for that message.
  `test_i18n_catalogs.py` ends with the negative controls: a wrong `Language:`, a plural index
  outside its forms or a form that is never chosen, a word taken out, a concept left out of the
  glossary, the glossary of one language applied to the catalog of another: each has to be reported.
