"""The shared web-source helpers (M8.3 part B; spec 3.3, 3.6; ADR-124 item 4).

``app/domain/web_sources.py`` is a new pure module. The four helpers that
``app/domain/question_bank.py`` had for web-found questions move there unchanged in behavior:
``_hidden`` (a control, line-separator or bidirectional-mark character), ``_text`` (one text of at
most 1000 characters without hidden characters, stripped, else ""), ``_source_date`` (a source's
own date text: at most 100 characters, stripped, None when blank, not text or holding a hidden
character) and ``_safe_url`` (http or https with a host, no userinfo, no whitespace or hidden
character, at most 2000 characters). ``question_bank`` imports them; it must not keep its own
copies. The existing question bank tests stay untouched and pass unchanged.
"""

import ast
import importlib
from pathlib import Path

import pytest


def _web_sources():
    # Imported when a test runs, so the missing module fails each test, not the collection.
    return importlib.import_module("app.domain.web_sources")


def _hidden(c):
    return _web_sources()._hidden(c)


def _text(value):
    return _web_sources()._text(value)


def _source_date(value):
    return _web_sources()._source_date(value)


def _safe_url(url):
    return _web_sources()._safe_url(url)


DOMAIN = Path(__file__).resolve().parents[3] / "app" / "domain"
# Code points are spelled out so editors and formatters can't turn them into invisible text.
HIDDEN_CODEPOINTS = [
    *(0x00, 0x09, 0x0A, 0x0D, 0x1F, 0x7F, 0x85, 0x9F),
    *(0x061C, 0x200E, 0x200F, 0x2028, 0x2029),
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
]
VISIBLE_CODEPOINTS = [0x20, 0x41, 0xA0, 0xE9, 0x2013, 0x200B, 0x4E2D, 0x1F600]


@pytest.mark.parametrize("codepoint", HIDDEN_CODEPOINTS)
def test_hidden_marks_controls_separators_and_bidi_characters(codepoint):
    assert _hidden(chr(codepoint)) is True


@pytest.mark.parametrize("codepoint", VISIBLE_CODEPOINTS)
def test_hidden_leaves_visible_characters_alone(codepoint):
    assert _hidden(chr(codepoint)) is False


def test_text_strips_and_keeps_ordinary_wording():
    assert _text("  A plain sentence.  ") == "A plain sentence."


@pytest.mark.parametrize(
    "value",
    [None, 5, b"bytes", ["list"], "", "   ", "\t", "a" + chr(0x200E) + "b", "a\nb", "a" + chr(0)],
)
def test_text_refuses_non_text_blank_and_hidden(value):
    assert _text(value) == ""


def test_text_limit_is_one_thousand_characters():
    assert _text("x" * 1000) == "x" * 1000
    assert _text("x" * 1001) == ""


@pytest.mark.parametrize("value", [None, 7, [], b"2024", "", "   "])
def test_source_date_is_none_for_blank_or_non_text(value):
    assert _source_date(value) is None


def test_source_date_strips_and_keeps_the_sources_own_words():
    assert _source_date("  last spring ") == "last spring"


def test_source_date_is_cut_to_one_hundred_characters():
    assert _source_date("d" * 150) == "d" * 100


@pytest.mark.parametrize("mark", [chr(0x200E), chr(0x202E), chr(0x2028), "\n", chr(0x7F)])
def test_source_date_with_a_hidden_character_is_none(mark):
    assert _source_date(f"spring{mark}2024") is None


@pytest.mark.parametrize(
    "url", ["https://example.test/a", "http://example.test", "https://example.test:8443/a?b=c#d"]
)
def test_safe_url_accepts_http_and_https_with_a_host(url):
    assert _safe_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<b>x</b>",
        "ftp://example.test/x",
        "file:///etc/hosts",
        "//example.test/x",
        "https://",
        "https:///path",
        "example.test/x",
        "",
        "https://user@example.test/x",
        "https://user:secret@example.test/x",
        "http://:secret@example.test/x",
        "https://example.test/a b",
        "https://example.test/a\tb",
        "https://example.test/a\nb",
        "https://example.test/a" + chr(0xA0) + "b",
        "https://example.test/a" + chr(0x200E),
        "https://example.test/a" + chr(0x202E),
        "https://example.test/a" + chr(0x7F),
        "https://example.test/a" + chr(0x2028),
        "http://[::1",
        "https://" + "a" * 2000,
    ],
)
def test_safe_url_refuses_other_schemes_userinfo_whitespace_hidden_and_long(url):
    assert _safe_url(url) is False


def test_safe_url_limit_is_two_thousand_characters():
    base = "https://example.test/"
    assert _safe_url(base + "a" * (2000 - len(base))) is True
    assert _safe_url(base + "a" * (2001 - len(base))) is False


def test_question_bank_uses_the_shared_helpers_and_keeps_no_copy():
    question_bank = importlib.import_module("app.domain.question_bank")
    for name in ("_text", "_source_date", "_safe_url"):
        assert getattr(question_bank, name) is getattr(_web_sources(), name), name


def test_question_bank_defines_none_of_the_moved_functions():
    tree = ast.parse((DOMAIN / "question_bank.py").read_text(encoding="utf-8"))
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert not defined & {"_hidden", "_text", "_source_date", "_safe_url"}


def test_web_sources_is_pure_stdlib_and_domain_only():
    tree = ast.parse((DOMAIN / "web_sources.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
    forbidden = (
        "fastapi",
        "httpx",
        "jinja2",
        "sqlite3",
        "starlette",
        "os",
        "pathlib",
        "socket",
        "subprocess",
        "random",
        "uuid",
        "app.modules",
        "app.services",
        "app.api",
        "app.tasks",
    )
    assert not [name for name in imported if name.split(".")[0] in forbidden or name in forbidden]


def test_question_bank_does_not_re_export_hidden():
    # Nothing outside web_sources and company_brief uses it; no unused re-export is kept.
    question_bank = importlib.import_module("app.domain.question_bank")
    assert not hasattr(question_bank, "_hidden")
