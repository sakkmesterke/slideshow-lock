"""What every catalog in ``po/`` has to be, whatever the language: the header says which language
it is, the plural rule is a rule that can be evaluated, no entry is fuzzy or empty, and the
translations use the words of the glossary of the language.

``test_i18n_translations.py`` has the checks that read the strings (complete, placeholders, shown
by the programs); this file has the ones that are about the catalog as a whole. Nothing is started:
no gettext, no GTK. Each check has a negative control further down: the same function, given a
catalog that is wrong in the way it looks for, has to say so.

Nobody who speaks these languages natively has read the translations. A passing test means the
catalog is whole and consistent with its glossary, not that the language is good.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests import i18n_catalogs as cat
from tests.i18n_catalogs import (
    CONCEPTS,
    FIRST_FIVE,
    GLOSSARY_DIR,
    WITHOUT_DATA_STRINGS,
    GlossaryError,
    PluralError,
    data_msgids,
    glossary_violations,
    header_fields,
    language_problems,
    parse_glossary,
    parse_plural_forms,
    plural_problems,
    read_catalog,
    same_plural_rule,
    shipped,
)

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("i18n_catalog", REPO / "tools" / "i18n_catalog.py")
catalog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(catalog)

#: The languages 1.0.1 ships: the five of 1.0.0 and the 35 that were added with it. A language
#: that is missing from ``po/LINGUAS`` fails ``test_the_requested_languages_are_all_shipped``.
REQUESTED = frozenset(
    "de es fr hu it "
    "bg ca cs da el et fi ga hr lt lv mt nl pl pt pt_BR ro sk sl sr sv nb "
    "uk ru ja zh_CN zh_TW ar he hi fil id th vi ko".split()
)

#: The plural rule of the languages the GNU gettext manual (the node "Plural forms") and
#: ``msginit`` (gettext 0.21) both know. The header of the catalog has to say the same: the same
#: number of forms and the same form for n = 0..1000, however it is written. Arabic, Irish and
#: Hebrew are here with the rule of that table; newer sources (Unicode CLDR) distinguish more forms
#: for Irish and Hebrew, which is a decision to make when the program uses ``ngettext``, and it
#: does not yet. Not in the table, so only checked to be evaluable: ca, fil, hi, id, mt, th, zh_*.
REFERENCE_PLURALS = {
    "de": "nplurals=2; plural=(n != 1);",
    "es": "nplurals=2; plural=(n != 1);",
    "fr": "nplurals=2; plural=(n > 1);",
    "hu": "nplurals=2; plural=(n != 1);",
    "it": "nplurals=2; plural=(n != 1);",
    "bg": "nplurals=2; plural=(n != 1);",
    "da": "nplurals=2; plural=(n != 1);",
    "el": "nplurals=2; plural=(n != 1);",
    "et": "nplurals=2; plural=(n != 1);",
    "fi": "nplurals=2; plural=(n != 1);",
    "nl": "nplurals=2; plural=(n != 1);",
    "nb": "nplurals=2; plural=(n != 1);",
    "sv": "nplurals=2; plural=(n != 1);",
    "pt": "nplurals=2; plural=(n != 1);",
    "pt_BR": "nplurals=2; plural=(n > 1);",
    "he": "nplurals=2; plural=(n != 1);",
    "cs": "nplurals=3; plural=(n==1) ? 0 : (n>=2 && n<=4) ? 1 : 2;",
    "sk": "nplurals=3; plural=(n==1) ? 0 : (n>=2 && n<=4) ? 1 : 2;",
    "pl": "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);",
    "ru": "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);",
    "uk": "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);",
    "sr": "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);",
    "hr": "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);",
    "lt": "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && (n%100<10 || n%100>=20) ? 1 : 2);",
    "lv": "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n != 0 ? 1 : 2);",
    "ro": "nplurals=3; plural=n==1 ? 0 : (n==0 || (n%100 > 0 && n%100 < 20)) ? 1 : 2;",
    "ga": "nplurals=3; plural=n==1 ? 0 : n==2 ? 1 : 2;",
    "sl": "nplurals=4; plural=(n%100==1 ? 0 : n%100==2 ? 1 : n%100==3 || n%100==4 ? 2 : 3);",
    "ar": "nplurals=6; plural=n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : n%100>=3 && n%100<=10 ? 3 : n%100>=11 ? 4 : 5;",
    "ja": "nplurals=1; plural=0;",
    "ko": "nplurals=1; plural=0;",
    "vi": "nplurals=1; plural=0;",
}

SHIPPED = shipped()
WITH_GLOSSARY = [lang for lang in SHIPPED if lang not in FIRST_FIVE]


def source_msgids():
    return {call.msgid for call in catalog.gettext_calls(REPO / "slideshow_lock")}


def glossary_of(lang):
    return parse_glossary((GLOSSARY_DIR / (lang + ".tsv")).read_text(encoding="utf-8"))


# -- which languages ---------------------------------------------------------------------------


def test_the_requested_languages_are_all_shipped():
    names = shipped()
    assert len(names) == len(set(names)), "a language twice in po/LINGUAS"
    assert set(names) == REQUESTED, (
        "missing: %s, not requested: %s"
        % (sorted(REQUESTED - set(names)), sorted(set(names) - REQUESTED))
    )


def test_the_catalogs_without_the_data_strings_really_lack_them():
    """``WITHOUT_DATA_STRINGS`` is the list of what is still open (the generated launcher and
    metadata are English there): it must not name a catalog that already has them."""
    new = data_msgids() - source_msgids()
    assert new, "the launcher and the metadata have no string of their own any more"
    for lang in sorted(WITHOUT_DATA_STRINGS):
        pairs, _lines = read_catalog(lang)
        have = {msgid for msgid, _msgstr in pairs}
        assert not (have & new), "%s has them: take it out of WITHOUT_DATA_STRINGS" % lang


# -- every catalog -----------------------------------------------------------------------------


@pytest.mark.parametrize("lang", SHIPPED)
def test_the_header_names_the_language_of_the_file(lang):
    pairs, _lines = read_catalog(lang)
    assert language_problems(pairs, lang) == []


@pytest.mark.parametrize("lang", SHIPPED)
def test_the_plural_rule_can_be_evaluated_and_stays_inside_its_forms(lang):
    pairs, _lines = read_catalog(lang)
    value = header_fields(pairs).get("Plural-Forms")
    assert value, "no Plural-Forms in the header"
    assert plural_problems(value) == []


@pytest.mark.parametrize("lang", sorted(REFERENCE_PLURALS))
def test_the_plural_rule_is_the_one_the_gettext_table_gives(lang):
    pairs, _lines = read_catalog(lang)
    value = header_fields(pairs)["Plural-Forms"]
    assert same_plural_rule(value, REFERENCE_PLURALS[lang]), value


@pytest.mark.parametrize("lang", SHIPPED)
def test_no_entry_is_fuzzy_obsolete_or_empty(lang):
    pairs, lines = read_catalog(lang)
    assert [line for line in lines if line.startswith("#,") and "fuzzy" in line] == []
    assert [line for line in lines if line.startswith("#~")] == []
    assert [msgid for msgid, msgstr in pairs if msgid and not msgstr.strip()] == []


# -- the glossary ------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", WITH_GLOSSARY)
def test_the_catalog_uses_the_words_of_its_glossary(lang):
    path = GLOSSARY_DIR / (lang + ".tsv")
    assert path.is_file(), "docs/translation-glossary/%s.tsv is missing" % lang
    pairs, _lines = read_catalog(lang)
    problems = glossary_violations(parse_glossary(path.read_text(encoding="utf-8")), pairs)
    assert problems == [], "\n".join(problems)


def test_every_concept_of_the_glossary_is_about_some_message():
    """A concept that no message mentions would check nothing."""
    assert cat.concepts_without_a_message(source_msgids() | data_msgids()) == []


def test_every_glossary_file_belongs_to_a_shipped_language():
    names = {path.stem for path in GLOSSARY_DIR.glob("*.tsv")}
    assert names <= set(SHIPPED), sorted(names - set(SHIPPED))


# -- negative controls: the checks above must be able to fail ----------------------------------


def test_control_a_header_that_names_another_language_is_reported():
    pairs, _lines = read_catalog("nl")
    assert language_problems(pairs, "nl") == []
    assert language_problems(pairs, "pl") != []
    wrong = [(msgid, msgstr.replace("Language: nl", "Language: de")) for msgid, msgstr in pairs]
    assert language_problems(wrong, "nl") != []
    assert language_problems([(m, s.replace("Language: nl\n", "")) for m, s in pairs], "nl") != []


@pytest.mark.parametrize(
    "value",
    [
        "nplurals=2; plural=(n%10==1 ? 0 : 2);",  # form 2 does not exist
        "nplurals=3; plural=(n==1 ? 0 : 1);",  # form 2 is never chosen
        "nplurals=2; plural=(n - 1);",  # -1 for n=0
        "nplurals=2; plural=(n != 1;",  # not a rule
        "nplurals=2; plural=__import__('os').system('true');",  # never evaluated as Python
        "nplurals=2; plural=n.real;",
        "nplurals=2; plural=(n / 0);",
        "plural=(n != 1);",
        "nplurals=two; plural=(n != 1);",
    ],
)
def test_control_a_plural_rule_that_is_wrong_is_reported(value):
    assert plural_problems(value) != []


def test_control_the_plural_parser_reads_c_precedence_and_the_reference_catches_a_wrong_rule():
    n_forms, evaluate = parse_plural_forms("nplurals=3; plural=n==1 ? 0 : n==2 ? 1 : 2;")
    assert (n_forms, [evaluate(n) for n in range(5)]) == (3, [2, 0, 1, 2, 2])
    _count, evaluate = parse_plural_forms("nplurals=2; plural=n!=1 && n!=2 || n==7;")
    assert [evaluate(n) for n in (0, 1, 2, 3, 7)] == [1, 0, 0, 1, 1]
    # the same rule written differently, and a different rule
    assert same_plural_rule("nplurals=2; plural=n>1 ? 1 : 0;", "nplurals=2; plural=(n > 1);")
    assert not same_plural_rule("nplurals=2; plural=(n != 1);", "nplurals=2; plural=(n > 1);")
    assert not same_plural_rule(
        "nplurals=3; plural=(n==1) ? 0 : (n>=2 && n<=4) ? 1 : 2;",
        REFERENCE_PLURALS["pl"],  # Czech and Slovak differ from Polish
    )
    with pytest.raises(PluralError):
        parse_plural_forms("nplurals=2; plural=open('x');")


def test_control_a_word_that_is_missing_or_a_concept_that_is_missing_is_reported():
    pairs, _lines = read_catalog("nl")
    glossary = glossary_of("nl")
    assert glossary_violations(glossary, pairs) == []

    # the Dutch word for "folder" taken out of the one message about the picture folder
    broken = [
        (msgid, msgstr.replace("map", "XXX") if msgid == "Choose the picture folder" else msgstr)
        for msgid, msgstr in pairs
    ]
    problems = glossary_violations(glossary, broken)
    assert any(problem.startswith("folder:") and "Choose the picture folder" in problem for problem in problems)
    broken = [(m, "XXX" if m == "No pictures to show" else s) for m, s in pairs]
    assert any(
        problem.startswith("picture:") and "No pictures to show" in problem
        for problem in glossary_violations(glossary, broken)
    )

    # a message that is about nothing of the glossary has nothing to hold against it
    unrelated = [(msgid, "XXX" if msgid == "Saved." else msgstr) for msgid, msgstr in pairs]
    assert glossary_violations(glossary, unrelated) == []

    # an empty translation and the product name left out
    empty = [(msgid, "" if msgid == "Preview" else msgstr) for msgid, msgstr in pairs]
    assert any(problem.startswith("preview:") for problem in glossary_violations(glossary, empty))
    unnamed = [(m, s.replace("Slideshow Lock", "Diavoorstelling")) for m, s in pairs]
    assert any(problem.startswith("product:") for problem in glossary_violations(glossary, unnamed))

    # a glossary without a concept
    without = {key: value for key, value in glossary.items() if key != "monitor"}
    assert "no line for the concept monitor" in glossary_violations(without, pairs)


def test_control_the_glossary_of_one_language_does_not_fit_the_catalog_of_another():
    """The check is about the words, not just the shape: Dutch words do not pass in Polish."""
    polish, _lines = read_catalog("pl")
    assert glossary_violations(glossary_of("nl"), polish) != []
    assert glossary_violations(glossary_of("pl"), polish) == []


def test_control_an_inflected_form_passes_through_the_alternatives_and_only_through_them():
    glossary = parse_glossary("slideshow\tpokaz\nlock\tblok")
    glossary = {**{c: (["x"], None) for c in CONCEPTS}, **glossary}
    pairs = [("Start the slideshow", "Uruchom pokazu"), ("Lock it", "Zablokuj")]
    # "pokaz" is a stem of "pokazu", "blok" of "zablokuj": both pass; "x" is not in the texts
    problems = glossary_violations(glossary, pairs)
    assert not any(problem.startswith(("slideshow:", "lock:")) for problem in problems)
    pairs = [("Start the slideshow", "Uruchom prezentację")]
    assert any(problem.startswith("slideshow:") for problem in glossary_violations(glossary, pairs))


def test_control_the_glossary_file_format():
    text = "# comment\n\nslideshow\ta|b\nlock\tc\tregex\n"
    assert parse_glossary(text) == {"slideshow": (["a", "b"], None), "lock": (["c"], "regex")}
    for bad in ("slideshow", "slideshow\t", "slideshow\ta\nslideshow\tb", "\tonly"):
        with pytest.raises(GlossaryError):
            parse_glossary(bad)
