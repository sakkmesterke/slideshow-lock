"""What the translatable strings in the source are, and what a template (.pot) holds.

stdlib only. ``tools/i18n.sh check`` and ``tests/test_i18n_source.py`` use it, so that the two
read the source the same way.

    python3 tools/i18n_catalog.py compare TEMPLATE.pot SOURCE_DIR
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set


class Call(NamedTuple):
    path: Path
    line: int
    msgid: Optional[str]  # None: the argument is not one plain string literal
    at_import: bool  # runs when the module is imported, before the language is selected


def _runs_at_import(node: ast.AST, parents: Dict[ast.AST, ast.AST]) -> bool:
    """True unless the call sits in the body of a function (default values and decorators run
    when the ``def`` does, a class body when the class is created)."""
    child = node
    while child in parents:
        parent = parents[child]
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and child in parent.body:
            return False
        if isinstance(parent, ast.Lambda) and child is parent.body:
            return False
        child = parent
    return True


def gettext_calls(source_dir: Path) -> List[Call]:
    """Every ``_(...)`` call in the ``*.py`` files under ``source_dir``."""
    calls = []
    for path in sorted(Path(source_dir).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "_":
                continue
            argument = node.args[0] if len(node.args) == 1 and not node.keywords else None
            literal = isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            calls.append(
                Call(
                    path,
                    node.lineno,
                    argument.value if literal else None,
                    _runs_at_import(node, parents),
                )
            )
    return calls


def pot_msgids(pot: Path) -> Set[str]:
    """The message ids of a template or catalog, without the header entry."""
    msgids: List[str] = []
    collecting = False
    for line in Path(pot).read_text(encoding="utf-8").splitlines():
        if line.startswith("msgid "):
            msgids.append(ast.literal_eval(line[len("msgid ") :]))
            collecting = True
        elif collecting and line.startswith('"'):
            msgids[-1] += ast.literal_eval(line)
        else:
            collecting = False
    return {msgid for msgid in msgids if msgid}


def compare(pot: Path, source_dir: Path) -> List[str]:
    """The problems, empty when every call is a plain literal that runs inside a function and
    the template holds exactly those strings."""
    problems = []
    calls = gettext_calls(source_dir)
    for call in calls:
        where = "%s:%d" % (call.path, call.line)
        if call.msgid is None:
            problems.append("%s: _() takes one plain string literal" % where)
        if call.at_import:
            problems.append("%s: _() runs at import, before the language is selected" % where)
    wanted = {call.msgid for call in calls if call.msgid}
    found = pot_msgids(pot)
    for msgid in sorted(wanted - found):
        problems.append("missing from the template: %r" % msgid)
    for msgid in sorted(found - wanted):
        problems.append("in the template but not in the source: %r" % msgid)
    print(
        "%d _() calls, %d distinct strings in the source, %d in the template"
        % (len(calls), len(wanted), len(found))
    )
    return problems


def main(argv: List[str]) -> int:
    if len(argv) != 4 or argv[1] != "compare":
        print(__doc__, file=sys.stderr)
        return 2
    problems = compare(Path(argv[2]), Path(argv[3]))
    for problem in problems:
        print("i18n: %s" % problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
