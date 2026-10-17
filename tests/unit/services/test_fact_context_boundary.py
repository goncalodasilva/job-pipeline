"""No service builds fact context for an AI request except through ``facts_for_ai()`` (M3.8;
AC-M3.4; spec 3.1 item 1; ADR-089 item 6).

Two rules, read from the source of ``app/`` (AST, no imports):

1. Fact references (``F12@3``) are made only in ``app/domain/facts.py`` (``FactRef``) and
   ``app/domain/fact_selection.py``: nowhere else constructs ``FactRef(...)`` or formats an
   ``F<n>@<v>`` string.
2. A module of ``app/services``, ``app/tasks`` or ``app/onboarding`` that calls ``run_task``
   with inputs about facts (a dict key or ``dict(...)`` keyword holding "fact") imports
   ``facts_for_ai`` from ``app.domain.fact_selection`` and calls it. The refine service (the
   module naming the task ``"refine_fact"``) is one of them.

The checkers are also run on small sources that break each rule, so each rule is proven to
bite.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
APP = REPO_ROOT / "app"
REF_MAKERS = frozenset({APP / "domain" / "facts.py", APP / "domain" / "fact_selection.py"})
CALLER_PACKAGES = (APP / "services", APP / "tasks", APP / "onboarding")
SELECTION_MODULE = "app.domain.fact_selection"


def _formats_a_ref(node: ast.AST) -> bool:
    """An f-string ``f"F{n}@{v}"``, or a literal format such as ``"F%d@%d"``/``"F{}@{}"``."""
    if isinstance(node, ast.JoinedStr):
        parts = node.values
        for index, part in enumerate(parts[:-2]):
            literal = isinstance(part, ast.Constant) and str(part.value).endswith("F")
            after = parts[index + 2]
            joined = isinstance(after, ast.Constant) and str(after.value).startswith("@")
            if literal and isinstance(parts[index + 1], ast.FormattedValue) and joined:
                return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return any(pattern in node.value for pattern in ("F%d@%d", "F%s@%s", "F{}@{}"))
    return False


def ref_problems(source: str, label: str) -> list[str]:
    """Rule 1 for one module's source."""
    problems = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else None
            name = node.func.id if isinstance(node.func, ast.Name) else name
            if name == "FactRef":
                problems.append(f"{label}:{node.lineno}: constructs FactRef")
        if _formats_a_ref(node):
            problems.append(f"{label}:{getattr(node, 'lineno', 0)}: formats an F<n>@<v> string")
    return problems


def _calls(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called == name:
                return True
    return False


def _fact_input_keys(tree: ast.AST) -> list[str]:
    keys = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys += [
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ]
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dict":
            keys += [keyword.arg for keyword in node.keywords if keyword.arg]
    return [key for key in keys if "fact" in key.casefold()]


def _imports_selector(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == SELECTION_MODULE
        and any(alias.name == "facts_for_ai" for alias in node.names)
        for node in ast.walk(tree)
    )


def selector_problems(source: str, label: str) -> list[str]:
    """Rule 2 for one module's source."""
    tree = ast.parse(source)
    if not _calls(tree, "run_task") or not _fact_input_keys(tree):
        return []
    if _imports_selector(tree) and _calls(tree, "facts_for_ai"):
        return []
    return [f"{label}: sends fact inputs to run_task without facts_for_ai()"]


def _names_refine_task(source: str) -> bool:
    return any(
        isinstance(node, ast.Constant) and node.value == "refine_fact"
        for node in ast.walk(ast.parse(source))
    )


def _modules(*folders: Path) -> list[Path]:
    return sorted(path for folder in folders for path in folder.rglob("*.py"))


def test_services_build_fact_context_only_through_selector() -> None:
    problems: list[str] = []
    for path in _modules(APP):
        if path not in REF_MAKERS:
            problems += ref_problems(path.read_text(encoding="utf-8"), str(path.relative_to(APP)))
    callers = _modules(*CALLER_PACKAGES)
    for path in callers:
        problems += selector_problems(path.read_text(encoding="utf-8"), str(path))

    assert problems == []
    # The rule has something to bite on: the refine service sends facts through the selector.
    refine = [p for p in callers if _names_refine_task(p.read_text(encoding="utf-8"))]
    assert refine, "no service under app/services or app/tasks names the refine_fact task"
    for path in refine:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _calls(tree, "run_task"):
            assert _imports_selector(tree) and _calls(tree, "facts_for_ai"), path
    assert (APP / "domain" / "fact_selection.py").is_file()


def test_ref_rule_bites() -> None:
    assert ref_problems("ref = FactRef(1, 2)\n", "m") != []
    assert ref_problems("from app.domain import facts\nr = facts.FactRef(1, 2)\n", "m") != []
    assert ref_problems('n, v = 1, 2\nr = f"F{n}@{v}"\n', "m") != []
    assert ref_problems('r = "F%d@%d" % (1, 2)\n', "m") != []
    assert ref_problems('r = "F{}@{}".format(1, 2)\n', "m") != []
    assert ref_problems('r = FactRef.parse("F1@2")\nlabel = f"Fact {r}"\n', "m") == []


def test_selector_rule_bites() -> None:
    bad = (
        "async def go(runner, rows):\n"
        "    inputs = {'facts': [row.text for row in rows], 'fact_id': 'F1@1'}\n"
        "    return await runner.run_task('some_task', inputs, None, meter=None)\n"
    )
    keyword = (
        "async def go(runner, rows):\n"
        "    return await runner.run_task('t', dict(context_facts=rows), None, meter=None)\n"
    )
    good = (
        "from app.domain.fact_selection import facts_for_ai\n"
        "async def go(runner, rows):\n"
        "    inputs = {'facts': facts_for_ai(rows)}\n"
        "    return await runner.run_task('some_task', inputs, None, meter=None)\n"
    )
    unrelated = (
        "async def go(runner, text):\n"
        "    return await runner.run_task('extract_cv', {'cv_text': text}, None, meter=None)\n"
    )

    assert selector_problems(bad, "m") != []
    assert selector_problems(keyword, "m") != []
    assert selector_problems(good, "m") == []
    assert selector_problems(unrelated, "m") == []
