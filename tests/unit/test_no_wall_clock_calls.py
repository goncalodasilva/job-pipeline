"""No wall-clock, uuid or random calls in app code outside clock.py and ids.py (spec 3.4).

Services get time and ids from the injected ``Clock`` and ``IdFactory``. This scanner
resolves import aliases and flags every reference, called or not (``default_factory=
datetime.now`` counts), to the wall clock, uuid generators and the ``random`` module.
Allowed on purpose: ``time.monotonic``/``time.perf_counter`` (durations, not dates) and
``secrets`` (launch token). A seeded ``random.Random`` is still flagged; inject it instead.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"
EXEMPT = {APP_DIR / "domain" / "clock.py", APP_DIR / "domain" / "ids.py"}

FORBIDDEN_CALLS = frozenset(
    {
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
        "time.time",
        "time.time_ns",
        "time.localtime",
        "time.gmtime",
        "time.ctime",
        "time.asctime",
        "time.strftime",
        "uuid.uuid1",
        "uuid.uuid4",
        "uuid.uuid6",
        "uuid.uuid7",
        "uuid.uuid8",
    }
)
FORBIDDEN_MODULE_PREFIXES = ("random.",)


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Map local names to the fully qualified names they were imported as."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    aliases[root] = root
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _dotted(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return list(reversed(parts))


def _is_forbidden(qualified: str) -> bool:
    return qualified in FORBIDDEN_CALLS or qualified.startswith(FORBIDDEN_MODULE_PREFIXES)


def forbidden_calls(path: Path) -> list[str]:
    """Return "<file>:<line> <qualified name>" for every forbidden reference in path."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = _import_aliases(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute | ast.Name) or not isinstance(node.ctx, ast.Load):
            continue
        parts = _dotted(node)
        if parts is None or parts[0] not in aliases:
            continue
        qualified = ".".join([aliases[parts[0]], *parts[1:]])
        if _is_forbidden(qualified):
            found.append(f"{path}:{node.lineno} {qualified}")
    return found


def test_no_wall_clock_or_uuid_calls_outside_clock_and_ids() -> None:
    files = [p for p in sorted(APP_DIR.rglob("*.py")) if p not in EXEMPT]
    assert files, "scanner found no app files; is APP_DIR right?"

    offences = [hit for path in files for hit in forbidden_calls(path)]

    assert offences == [], "Use the injected Clock/IdFactory instead (spec 3.4):\n" + "\n".join(
        offences
    )


FORBIDDEN_SNIPPETS = {
    "datetime_class_now": "from datetime import datetime\nx = datetime.now()\n",
    "datetime_class_utcnow": "from datetime import datetime\nx = datetime.utcnow()\n",
    "datetime_class_today": "from datetime import datetime\nx = datetime.today()\n",
    "date_today": "from datetime import date\nx = date.today()\n",
    "module_datetime_now": "import datetime\nx = datetime.datetime.now()\n",
    "module_alias_now": "import datetime as dt\nx = dt.datetime.now(dt.UTC)\n",
    "module_date_today": "import datetime\nx = datetime.date.today()\n",
    "class_alias_now": "from datetime import datetime as DT\nx = DT.now()\n",
    "time_time": "import time\nx = time.time()\n",
    "uuid_uuid4": "import uuid\nx = uuid.uuid4()\n",
    "uuid_uuid1": "import uuid\nx = uuid.uuid1()\n",
    "from_uuid_import_uuid4": "from uuid import uuid4\nx = uuid4()\n",
    "from_uuid_alias": "from uuid import uuid4 as make\nx = make()\n",
    "random_module": "import random\nx = random.random()\n",
    "random_alias": "import random as r\nx = r.choice([1, 2])\n",
    "from_random_import": "from random import randint\nx = randint(1, 6)\n",
    "default_factory_reference": (
        "from dataclasses import dataclass, field\nfrom datetime import datetime\n\n"
        "@dataclass\nclass Row:\n    at: datetime = field(default_factory=datetime.now)\n"
    ),
    "bare_reference_then_call": "import uuid\nmake = uuid.uuid4\nx = make()\n",
    "uuid7": "import uuid\nx = uuid.uuid7()\n",
    "time_localtime": "import time\nx = time.localtime()\n",
    "time_strftime_implicit_now": "import time\nx = time.strftime('%Y')\n",
    "nested_in_function": (
        "from datetime import datetime\n\n"
        "def stamp() -> str:\n"
        "    return datetime.now().isoformat()\n"
    ),
}

ALLOWED_SNIPPETS = {
    "fixed_datetime_and_timedelta": (
        "from datetime import UTC, datetime, timedelta\n"
        "x = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=1)\n"
    ),
    "parsing_and_uuid_parse": (
        "import uuid\nfrom datetime import date, datetime\n"
        "a = datetime.fromisoformat('2026-01-01T00:00:00+00:00')\n"
        "b = date(2026, 1, 1).isoformat()\n"
        "c = uuid.UUID('12345678-1234-4678-9234-567812345678')\n"
    ),
    "injected_clock_and_ids": ("def run(clock, ids):\n    return clock.now(), ids.new()\n"),
    "unrelated_now_method": "class C:\n    def now(self): ...\nx = C().now()\n",
    "durations_and_tokens": (
        "import secrets\nimport time\n"
        "start = time.monotonic()\nelapsed = time.perf_counter() - start\n"
        "token = secrets.token_urlsafe(32)\n"
    ),
}


@pytest.mark.parametrize("source", FORBIDDEN_SNIPPETS.values(), ids=FORBIDDEN_SNIPPETS.keys())
def test_scanner_detects_forbidden_call(tmp_path: Path, source: str) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")

    assert len(forbidden_calls(sample)) == 1


@pytest.mark.parametrize("source", ALLOWED_SNIPPETS.values(), ids=ALLOWED_SNIPPETS.keys())
def test_scanner_allows_deterministic_code(tmp_path: Path, source: str) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")

    assert forbidden_calls(sample) == []
