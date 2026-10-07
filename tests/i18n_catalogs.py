"""Reading the real catalogs in ``po/`` and the glossaries in ``docs/translation-glossary/``.

Shared by ``test_i18n_translations.py`` and ``test_i18n_catalogs.py``. stdlib only: no gettext, no
GTK, nothing is started.

Three checks have a rule of their own, written down here:

* ``Plural-Forms``: the expression is read by a small parser (never ``eval``), the characters it
  may hold are checked first, and it is evaluated for ``n`` = 0..200 (``plural_problems``).
* The glossary (``glossary_violations``): ``docs/translation-glossary/<lang>.tsv`` names, for each
  key concept, the one translation the catalog uses, and every message that is about the concept
  has to use it. The rule is in the docstring of that function and in ``docs/translations.md``.
* The data strings (``data_msgids``): the launcher and the AppStream metadata, read as text.
"""

from __future__ import annotations

import ast
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

REPO = Path(__file__).resolve().parent.parent
PO = REPO / "po"
DATA = REPO / "data"
GLOSSARY_DIR = REPO / "docs" / "translation-glossary"

#: The product name stays as it is in every catalog.
PRODUCT = "Slideshow Lock"

#: The five catalogs of 1.0.0: German, Spanish, French, Hungarian and Italian. They are the ones
#: that no glossary exists for, and that name no AI in the header.
FIRST_FIVE = frozenset({"de", "es", "fr", "hu", "it"})

#: The catalogs that do not hold the five strings of the launcher and the metadata
#: (``tools/i18n.sh update`` adds them empty), so the generated launcher and metadata are English
#: in these languages; every other catalog holds all of them. A language leaves this set when its
#: catalog gets the five strings.
WITHOUT_DATA_STRINGS = frozenset(FIRST_FIVE)


def linguas(text: str) -> List[str]:
    return [name for line in text.splitlines() for name in line.split("#")[0].split()]


def shipped() -> List[str]:
    return sorted(linguas((PO / "LINGUAS").read_text(encoding="utf-8")))


def po_path(lang: str) -> Path:
    return PO / (lang + ".po")


def parse_po(text: str):
    """``(pairs, lines)``: ``(msgid, msgstr)`` of every entry, the header (msgid "") first, and
    the raw lines."""
    lines = text.split("\n")
    pairs, field, current = [], None, {"msgid": "", "msgstr": ""}
    for line in lines + ['msgid ""']:
        if line.startswith("msgid "):
            if field is not None:
                pairs.append((current["msgid"], current["msgstr"]))
            current, field = {"msgid": ast.literal_eval(line[6:]), "msgstr": ""}, "msgid"
        elif line.startswith("msgstr "):
            current["msgstr"], field = ast.literal_eval(line[7:]), "msgstr"
        elif line.startswith('"') and field is not None:
            current[field] += ast.literal_eval(line)
    return pairs, lines


def read_catalog(lang: str):
    return parse_po(po_path(lang).read_text(encoding="utf-8"))


def header_fields(pairs: Sequence[Tuple[str, str]]) -> Dict[str, str]:
    """The ``Key: value`` lines of the header entry (the entry whose msgid is empty)."""
    header = next(msgstr for msgid, msgstr in pairs if msgid == "")
    fields = {}
    for line in header.split("\n"):
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def language_problems(pairs: Sequence[Tuple[str, str]], lang: str) -> List[str]:
    got = header_fields(pairs).get("Language")
    return [] if got == lang else ["Language is %r, the file is %s.po" % (got, lang)]


def data_msgids() -> Set[str]:
    """The strings the launcher and the AppStream metadata hand to the translators: ``Name`` and
    ``Comment`` of the ``.desktop.in``, and name, summary and each description paragraph of the
    ``.metainfo.xml.in`` (a paragraph is one string, its white space collapsed). Nothing else
    (the ``Keywords`` line is not translated)."""
    found: Set[str] = set()
    for path in DATA.glob("*.desktop.in"):
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key in ("Name", "Comment"):
                found.add(value.strip())
    for path in DATA.glob("*.metainfo.xml.in"):
        root = ET.parse(path).getroot()
        elements = [root.find("name"), root.find("summary")] + list(root.findall("description/p"))
        found.update(" ".join(element.text.split()) for element in elements if element is not None)
    return found


# -- Plural-Forms --------------------------------------------------------------------------------

_PLURAL_HEADER = re.compile(r"^nplurals\s*=\s*(\d+)\s*;\s*plural\s*=\s*(.+?)\s*;?\s*$")
_PLURAL_CHARS = re.compile(r"^[n0-9%!=<>&|?:()+*/\s-]+$")
_TOKEN = re.compile(r"\s*(\d+|n|&&|\|\||==|!=|<=|>=|[()?:%!<>+*/-])")


class PluralError(ValueError):
    pass


def _tokens(expression: str) -> List[str]:
    out, pos = [], 0
    while pos < len(expression):
        match = _TOKEN.match(expression, pos)
        if match is None:
            if expression[pos:].strip() == "":
                break
            raise PluralError("cannot read %r" % expression[pos:])
        out.append(match.group(1))
        pos = match.end()
    return out


class _Parser:
    """The expression of a Plural-Forms header: C syntax, one variable ``n``, no negative numbers.
    Precedence as in C. The result is a nested tuple that ``_value`` evaluates."""

    def __init__(self, tokens: List[str]) -> None:
        self.tokens, self.pos = tokens, 0

    def peek(self) -> Optional[str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, *expected: str) -> Optional[str]:
        token = self.peek()
        if token is not None and (not expected or token in expected):
            self.pos += 1
            return token
        return None

    def parse(self):
        tree = self.conditional()
        if self.peek() is not None:
            raise PluralError("unexpected %r" % self.peek())
        return tree

    def conditional(self):
        test = self.binary(0)
        if self.take("?"):
            yes = self.conditional()
            if not self.take(":"):
                raise PluralError("missing ':'")
            return ("?", test, yes, self.conditional())
        return test

    LEVELS = (("||",), ("&&",), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-"), ("*", "/", "%"))

    def binary(self, level: int):
        if level == len(self.LEVELS):
            return self.unary()
        left = self.binary(level + 1)
        while True:
            operator = self.take(*self.LEVELS[level])
            if operator is None:
                return left
            left = (operator, left, self.binary(level + 1))

    def unary(self):
        if self.take("!"):
            return ("!", self.unary())
        if self.take("("):
            tree = self.conditional()
            if not self.take(")"):
                raise PluralError("missing ')'")
            return tree
        token = self.take()
        if token == "n":
            return ("n",)
        if token is not None and token.isdigit():
            return ("num", int(token))
        raise PluralError("unexpected %r" % token)


def _value(tree, n: int) -> int:
    kind = tree[0]
    if kind == "n":
        return n
    if kind == "num":
        return tree[1]
    if kind == "!":
        return int(not _value(tree[1], n))
    if kind == "?":
        return _value(tree[2], n) if _value(tree[1], n) else _value(tree[3], n)
    if kind == "&&":
        return int(bool(_value(tree[1], n)) and bool(_value(tree[2], n)))
    if kind == "||":
        return int(bool(_value(tree[1], n)) or bool(_value(tree[2], n)))
    left, right = _value(tree[1], n), _value(tree[2], n)
    if kind in ("/", "%") and right == 0:
        raise PluralError("division by zero")
    return {
        "==": lambda: int(left == right),
        "!=": lambda: int(left != right),
        "<": lambda: int(left < right),
        ">": lambda: int(left > right),
        "<=": lambda: int(left <= right),
        ">=": lambda: int(left >= right),
        "+": lambda: left + right,
        "-": lambda: left - right,
        "*": lambda: left * right,
        "/": lambda: left // right,
        "%": lambda: left % right,
    }[kind]()


def parse_plural_forms(value: str):
    """``(nplurals, evaluate)`` for the value of a ``Plural-Forms`` header; PluralError when it is
    not a plain ``nplurals=N; plural=EXPRESSION;`` of the characters a C plural expression has."""
    match = _PLURAL_HEADER.match(value.strip())
    if match is None:
        raise PluralError("not 'nplurals=N; plural=EXPRESSION;': %r" % value)
    expression = match.group(2)
    if not _PLURAL_CHARS.match(expression):
        raise PluralError("characters that a plural expression does not have: %r" % expression)
    tree = _Parser(_tokens(expression)).parse()
    return int(match.group(1)), lambda n: _value(tree, n)


def plural_problems(value: str, limit: int = 200) -> List[str]:
    """What is wrong with a Plural-Forms value: unreadable, a result outside 0..nplurals-1 for some
    n in 0..*limit*, or a form that no n in that range reaches (nplurals then says more than the
    expression can do)."""
    try:
        nplurals, evaluate = parse_plural_forms(value)
        results = [evaluate(n) for n in range(limit + 1)]
    except PluralError as error:
        return [str(error)]
    problems = []
    outside = sorted({r for r in results if not 0 <= r < nplurals})
    if outside:
        problems.append("index %s outside 0..%d" % (outside, nplurals - 1))
    unused = sorted(set(range(nplurals)) - set(results))
    if unused:
        problems.append("form %s is never chosen for n=0..%d" % (unused, limit))
    return problems


def same_plural_rule(value: str, reference: str, limit: int = 1000) -> bool:
    """True when both headers say the same: the same nplurals and the same form for n=0..*limit*."""
    nplurals, evaluate = parse_plural_forms(value)
    ref_nplurals, ref_evaluate = parse_plural_forms(reference)
    return nplurals == ref_nplurals and all(
        evaluate(n) == ref_evaluate(n) for n in range(limit + 1)
    )


# -- the glossary --------------------------------------------------------------------------------

#: The concepts every glossary names, with the English words that make a message "about" them.
#: Matched on the message with the product name and the command lines taken out.
CONCEPTS = {
    "slideshow": r"\bslideshow\b",
    "lock": r"\block(?:s|ed|ing)?\b",
    "preview": r"\bpreview\b",
    "settings": r"\bsettings?\b",
    "idle": r"\bidle\b",
    "folder": r"\bfolders?\b",
    "picture": r"\bpictures?\b",
    "fullscreen": r"\bfullscreen\b",
    "monitor": r"\bmonitors?\b",
    "product": re.escape(PRODUCT),
}


#: Other names a glossary gives to a concept (the product name is a concept too).
CONCEPT_ALIASES = {"slideshow lock": "product", "product name": "product"}


class GlossaryError(ValueError):
    pass


def parse_glossary(text: str) -> Dict[str, Tuple[List[str], Optional[str]]]:
    """``{concept: (translations, english pattern or None)}`` from the text of a ``.tsv``.

    One concept per line, tab separated: the concept, the translation(s) and, for a concept that
    is not one of ``CONCEPTS``, the regular expression for the English words (optional; for the
    concepts of ``CONCEPTS`` the expressions here decide, a third column is not read). The
    concept is read without regard to case, and ``Slideshow Lock`` means ``product``. Several
    translations are written ``a|b`` (forms of one word, or its stem for a language that inflects:
    the message passes when it holds any of them). Blank lines and lines starting with ``#`` are
    skipped."""
    glossary: Dict[str, Tuple[List[str], Optional[str]]] = {}
    for number, line in enumerate(text.split("\n"), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = line.split("\t")
        if len(cells) < 2 or not cells[0].strip():
            raise GlossaryError("line %d: expected concept<TAB>translation" % number)
        concept = cells[0].strip().lower()
        concept = CONCEPT_ALIASES.get(concept, concept)
        translations = [part.strip() for part in cells[1].split("|") if part.strip()]
        if not translations:
            raise GlossaryError("line %d: no translation for %s" % (number, concept))
        if concept in glossary:
            raise GlossaryError("line %d: %s twice" % (number, concept))
        pattern = cells[2].strip() if len(cells) > 2 and cells[2].strip() else None
        glossary[concept] = (translations, pattern)
    return glossary


def _about(msgid: str, concept: str, pattern: str) -> bool:
    text = "\n".join(line for line in msgid.split("\n") if not line.startswith("  "))
    if concept != "product":
        text = text.replace(PRODUCT, " ")
    return re.search(pattern, text, re.IGNORECASE) is not None


def glossary_violations(
    glossary: Dict[str, Tuple[List[str], Optional[str]]], pairs: Iterable[Tuple[str, str]]
) -> List[str]:
    """What breaks the glossary of a catalog. The rule:

    * every concept of ``CONCEPTS`` has a line in the glossary;
    * a message (msgid) is *about* a concept when the English words of the concept are in it, after
      the product name ``Slideshow Lock`` and the indented command lines are taken out (for the
      concept ``product`` itself the name is looked for as it is);
    * the translation (msgstr) of such a message holds at least one of the translations of the
      concept, compared without regard to upper and lower case. A message that is not about the
      concept is not looked at: the word may be used there too, but nothing requires it;
    * the header entry (empty msgid) is not a message.
    """
    problems = [
        "no line for the concept %s" % concept for concept in CONCEPTS if concept not in glossary
    ]
    for msgid, msgstr in pairs:
        if not msgid:
            continue
        shown = "\n".join(
            line for line in msgstr.split("\n") if not line.startswith("  ")
        ).casefold()
        for concept, (translations, pattern) in glossary.items():
            pattern = CONCEPTS.get(concept, pattern)
            if pattern is None or not _about(msgid, concept, pattern):
                continue
            if not any(word.casefold() in shown for word in translations):
                problems.append(
                    "%s: %r does not use %s" % (concept, msgid, " or ".join(translations))
                )
    return problems


def concepts_without_a_message(msgids: Iterable[str]) -> List[str]:
    """The concepts of ``CONCEPTS`` that no message is about: a concept like that checks nothing."""
    msgids = list(msgids)
    return [
        concept
        for concept, pattern in CONCEPTS.items()
        if not any(_about(msgid, concept, pattern) for msgid in msgids)
    ]
