"""M9.3 part B: Settings > Prompts (AC-M9.2; ADR-055; ADR-131 item 7; spec 2.9 rules 2, 3, 12 and
13, 3.3 and 3.6): a token-free list of every task, a token-free shell page per task and a
protected editor fragment, with saves and resets that are POSTs carrying the launch token, the
origin and the override's content version.

The routes, the markup, the copy and the status codes are described in ``tests/prompt_pages.py``.
Real SQLite and prompt folders in a temp folder, the app through ``TestClient``, no network. The
library and editor of part A (``app.state.prompt_editor``) are used to read versions and to put
override files in place. Every value is fictional. Names are imported inside the tests that need
them where an application module is missing, so each test fails on its own.
"""

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, get_args
from urllib.parse import quote

import pytest
from fastapi.routing import APIRoute

from app.api.prompt_forms import REFUSAL_FALLBACK, REFUSAL_MESSAGES
from app.api.security import PAGE_TAG
from app.domain.errors import VersionConflict
from app.domain.tiers import DEFAULT_TIERS
from app.modules.ai import RefusalReason
from tests.board_pages import by_id, elements
from tests.data_pages import get_token, state_of
from tests.integration.test_settings_areas import (
    HTMX,
    area_app,
    main_of,
    nav_links,
)
from tests.integration.test_setup_redirect import ORIGIN, Running
from tests.lists_pages import ids_are_unique, label_text
from tests.prompt_override_support import snapshot, write_override_bytes
from tests.prompt_pages import (
    BADGE_WORDS,
    INVALID,
    LEAK,
    MARKER,
    NO_VERSION,
    NOT_SAVED,
    NOTICE_WORDS,
    PATH,
    RESET,
    SAVED,
    STALE,
    TASK,
    TASKS,
    UNTRUSTED_NAME,
    autofocus,
    badges_in,
    block_text,
    by_known_id,
    default_updated,
    diff_lines,
    edited_on,
    editor_path,
    editor_section,
    field_error,
    form_version,
    front_version,
    has_announce,
    invalid_fields,
    label_of,
    list_rows,
    one_line,
    operable,
    reset_path,
    row_of,
    said_of,
    same_text,
    task_path,
    textarea_text,
    using_default,
)

pytestmark = pytest.mark.integration

EXTRA = "Fictional extra instruction from the user, so the reader can tell the override apart."
HOSTILE_LINE = '<script id="injected-diff">1</script> {# not a comment here #} </textarea>'
POSTING_TEXTS = (
    "Platform Engineer at Harbor Kite Fictional. Operate the deployment pipeline. "
    "Four years of Linux administration. Remote within the EU.",
    "Kite Rigger at Lantern Quay Fictional. Maintain tethers and sails for the harbour team. "
    "Two years of rigging experience. On site in Fictionville.",
)
NAV_LABELS = [
    "Providers and AI",
    "Usage budgets",
    "Job preferences",
    "Lists and statuses",
    "Prompts",
    "Sessions",
    "Your details and CV templates",
    "Appearance",
    "Data and privacy",
    "Keyboard shortcuts",
]


class Prompts:
    """The running app plus what the tests do to the prompt folders and the routes."""

    def __init__(self, run: Running, root: Path) -> None:
        self.run = run
        self.root = root
        self.data = root / "data"
        self.overrides = self.data / "prompts" / "overrides"

    # --- the library of part A ---------------------------------------------------------------

    @property
    def editor(self) -> Any:
        editor = self.run.app.state.prompt_editor
        assert editor is not None, "app.state.prompt_editor is published with the workspace"
        return editor

    def status(self, task: str) -> Any:
        return self.editor.status(task)

    def default_version(self, task: str) -> int:
        return int(self.status(task).default_version)

    def versioned_task(self) -> str:
        """The first task whose default is above version 1, so an older base version exists."""
        for task in TASKS:
            if self.default_version(task) >= 2:
                return task
        raise AssertionError("no shipped prompt above version 1")

    def file(self, task: str) -> Path:
        return self.overrides / task / "prompt.md"

    def edit(self, task: str, extra: str = EXTRA) -> tuple[str, str]:
        """Save the default with ``extra`` added to the system section (through the editor)."""
        default = self.editor.default_text(task)
        system = f"{default.system}\n{extra}"
        self.editor.save_override(task, system, default.user, expected_content_version="none")
        return system, default.user

    def lower(self, task: str, base: int) -> str:
        """Rewrite the saved override as if it had been made against default version ``base``."""
        path = self.file(task)
        data = path.read_bytes()
        changed = re.sub(rb"(?m)^version: \d+$", f"version: {base}".encode(), data, count=1)
        assert changed != data, "the base version must differ from the file's"
        path.write_bytes(changed)
        status = self.status(task)
        assert status.base_version == base and status.error is None
        return str(status.content_version)

    def put_bytes(self, task: str, data: bytes) -> None:
        write_override_bytes(self.overrides, data, task=task)

    # --- what is on disk and in the database -------------------------------------------------

    def state(self) -> tuple[Any, Any]:
        return state_of(self.run, self.data), snapshot(self.overrides)

    # --- requests ----------------------------------------------------------------------------

    def get(self, path: str, *, htmx: bool = False, token: bool = False) -> Any:
        if htmx:
            return get_token(self.run, path, htmx=True)
        return get_token(self.run, path) if token else self.run.client.get(path)

    def fragment(self, task: str) -> Any:
        response = get_token(self.run, editor_path(task))
        assert response.status_code == 200, (task, response.status_code)
        return response

    def post(self, path: str, data: Any, *, htmx: bool = True) -> Any:
        return self.run.client.post(
            path, data=data, headers=self.run.action(**(HTMX if htmx else {}))
        )

    def form(self, task: str, **values: Any) -> dict[str, Any]:
        """The save form as the fragment would send it, except for ``values``."""
        default = self.editor.default_text(task)
        form: dict[str, Any] = {
            "version": self.status(task).content_version,
            "system": f"{default.system}\n{EXTRA}",
            "user": default.user,
        }
        form.update(values)
        return {name: value for name, value in form.items() if value is not None}

    def save(self, task: str, *, htmx: bool = True, **values: Any) -> Any:
        return self.post(task_path(task), self.form(task, **values), htmx=htmx)

    def reset(self, task: str, *, htmx: bool = True, **values: Any) -> Any:
        form: dict[str, Any] = {"version": self.status(task).content_version, **values}
        return self.post(reset_path(task), form, htmx=htmx)


@contextmanager
def prompts_app(tmp_path: Path) -> Iterator[Prompts]:
    root = tmp_path / "root"
    with area_app(root) as run:
        yield Prompts(run, root)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- the area: side list and list page ---------------------------------------------------------


def test_prompts_is_listed_between_lists_and_sessions_in_the_side_list(tmp_path: Path) -> None:
    """The side list has ten links now (was nine): Prompts sits after "Lists and statuses" and
    before "Sessions", as in the design table, and links to ``/settings/prompts``."""
    with prompts_app(tmp_path) as p:
        for markup in (p.get("/settings").text, p.get(PATH, htmx=True).text):
            links = nav_links(markup)
            assert [link.text for link in links] == NAV_LABELS
            prompts = next(link for link in links if link.text == "Prompts")
            assert prompts.attrs.get("href") == PATH
        assert p.get(PATH).status_code == 200


def test_prompts_list_has_every_task_in_order_with_plain_labels_and_versions(
    tmp_path: Path,
) -> None:
    """One row per task of ``DEFAULT_TIERS`` in that order: the plain label as the one link to
    the task page, the default version as text, no badge on a fresh install, nothing to operate
    and no prompt text. The htmx content is the same list."""
    with prompts_app(tmp_path) as p:
        for label, response in (
            ("page", p.get(PATH)),
            ("token only", p.get(PATH, token=True)),
            ("htmx", p.get(PATH, htmx=True)),
        ):
            assert response.status_code == 200, label
            markup = response.text
            section = by_known_id(markup, "prompts-settings")
            assert section.tag == "section"
            assert section.attrs.get("aria-labelledby") == "prompts-heading"
            assert by_known_id(markup, "prompts-heading").text == "Prompts"
            rows = list_rows(markup)
            assert [str(r.attrs["data-task"]) for r in rows] == list(DEFAULT_TIERS), label
            for task, row in zip(DEFAULT_TIERS, rows, strict=True):
                assert row.attrs.get("id") == f"prompt-{task}"
                links = [e for e in elements(markup) if e.tag == "a" and e.within(row)]
                assert [(e.attrs.get("id"), e.attrs.get("href"), e.text) for e in links] == [
                    (f"prompt-link-{task}", task_path(task), label_of(task))
                ], (label, task)
                assert f"v{p.default_version(task)}" in row.text, (label, task, row.text)
                assert badges_in(markup, row) == [], (label, task)
            assert operable(markup, section) == [], f"{label}: the list can't be operated"
            assert ids_are_unique(markup) is None
            assert "<pre" not in markup and "<textarea" not in markup
            if label == "htmx":
                assert "<html" not in markup
        # no prompt text on the page
        for task in (TASK, p.versioned_task()):
            first_line = p.editor.default_text(task).system.splitlines()[0]
            assert first_line not in p.get(PATH).text


def test_prompts_list_badges_come_from_the_override_files(tmp_path: Path) -> None:
    """ "Edited" (an override exists), "Default updated" (its base version is not the default
    version) and "Can't load" (the override can't be loaded), as text on the list and on the
    task page shell. Only the tasks whose files were put there carry a badge."""
    with prompts_app(tmp_path) as p:
        edited, updated = TASK, p.versioned_task()
        unsafe, garbage = "assess_fit", "gap_check"
        assert len({edited, updated, unsafe, garbage}) == 4
        p.edit(edited)
        p.edit(updated)
        base = p.default_version(updated) - 1
        p.lower(updated, base)
        p.edit(unsafe)
        data = p.file(unsafe).read_bytes()
        p.put_bytes(unsafe, data.replace(b"\n## user\n\n", b"\n## user\n\n{{ kestrel_x }}\n", 1))
        assert p.status(unsafe).error, "the crafted override can't be loaded"
        p.put_bytes(garbage, b"this is not a prompt file")
        assert p.status(garbage).error

        markup = p.get(PATH).text
        shown = {task: badges_in(markup, row_of(markup, task)) for task in TASKS}
        assert shown[edited] == [BADGE_WORDS["edited"]]
        assert sorted(shown[updated]) == sorted(
            [BADGE_WORDS["edited"], BADGE_WORDS["default-updated"]]
        )
        for task in (unsafe, garbage):
            assert BADGE_WORDS["cant-load"] in shown[task], task
            assert BADGE_WORDS["default-updated"] not in shown[task], task
        for task in TASKS:
            if task not in (edited, updated, unsafe, garbage):
                assert shown[task] == [], task
        # the same words on the task page shell, which also names the base version
        shell = p.get(task_path(updated)).text
        assert sorted(badges_in(shell)) == sorted(shown[updated])
        base_text = by_known_id(shell, "prompt-base-version").text
        assert f"v{base}" in base_text, base_text
        assert BADGE_WORDS["cant-load"] in badges_in(p.get(task_path(garbage)).text)
        assert badges_in(p.get(task_path("assess_fit")).text).count("Can't load") == 1
        assert by_id(p.get(task_path("extract_cv")).text, "prompt-base-version") is None


# --- the task page: a token-free shell --------------------------------------------------------


@pytest.mark.parametrize("state", ["default", "edited", "default_updated"])
def test_prompt_task_page_is_a_shell_that_loads_the_protected_editor(
    tmp_path: Path, state: str
) -> None:
    """Spec 2.9 rule 13: the token-free page holds the heading, the label, the versions and the
    badges, plus a placeholder that fetches the protected editor on load. It has no form and no
    prompt text; the htmx content (token needed) is the same shell without ``<html>``."""
    with prompts_app(tmp_path) as p:
        task = p.versioned_task()
        default = p.default_version(task)
        if state != "default":
            p.edit(task)
        if state == "default_updated":
            p.lower(task, default - 1)
        text = p.editor.default_text(task)
        assert p.get(task_path(task), htmx=False).status_code == 200
        assert p.run.client.get(task_path(task), headers=HTMX).status_code == 403
        for label, response in (
            ("page", p.get(task_path(task))),
            ("head", p.run.client.head(task_path(task))),
            ("htmx", p.get(task_path(task), htmx=True)),
        ):
            assert response.status_code == 200, label
            if label == "head":
                continue
            markup = response.text
            assert ("<html" in markup) == (label == "page"), label
            heading = [e for e in main_of(markup) if e.tag == "h1"]
            assert [(h.attrs.get("id"), h.text) for h in heading] == [
                ("page-heading", label_of(task))
            ]
            assert autofocus(markup) == heading, f"{label}: the heading is the one focus target"
            links = nav_links(markup)
            assert [link.text for link in links] == NAV_LABELS
            current = [link for link in links if link.attrs.get("aria-current") in ("page", "true")]
            assert [link.text for link in current] == ["Prompts"], label
            version_line = by_known_id(markup, "prompt-default-version").text
            assert f"v{default}" in version_line, version_line
            expected = {
                "default": [],
                "edited": [BADGE_WORDS["edited"]],
                "default_updated": [BADGE_WORDS["edited"], BADGE_WORDS["default-updated"]],
            }[state]
            assert sorted(badges_in(markup)) == sorted(expected), (label, state)
            base = by_id(markup, "prompt-base-version")
            if state == "default":
                assert base is None
            else:
                wanted = default - 1 if state == "default_updated" else default
                assert base is not None and f"v{wanted}" in base.text, (state, base)
            holder = editor_section(markup)
            assert holder.attrs.get("hx-get") == editor_path(task)
            assert "load" in str(holder.attrs.get("hx-trigger"))
            assert holder.attrs.get("hx-swap") == "outerHTML"
            assert holder.text, "the placeholder says it is loading"
            assert not [e for e in main_of(markup) if e.tag in ("form", "input", "textarea")]
            for line in (text.system.splitlines()[0], text.user.splitlines()[0]):
                assert line not in markup, f"{label}: no prompt text on the token-free page"
            assert LEAK not in markup and EXTRA not in markup
        title = re.findall(r"<title>(.*?)</title>", p.get(task_path(task)).text, re.DOTALL)
        assert len(title) == 1 and title[0].startswith(label_of(task)), title
        assert "Prompts" in title[0] and "Settings" in title[0], title
        assert title[0].endswith("Job Search Pipeline"), title


def test_prompt_task_page_is_a_page_route_and_the_editor_is_not(tmp_path: Path) -> None:
    with prompts_app(tmp_path) as p:
        routes = [r for r in p.run.app.routes if isinstance(r, APIRoute)]
        pages = {r.path: r for r in routes if PAGE_TAG in (r.tags or [])}
        assert {"GET", "HEAD"} <= pages["/settings/prompts/{task}"].methods
        assert "/settings/prompts" in pages
        by_path = {r.path: r for r in routes}
        for path in ("/settings/prompts/{task}/editor",):
            assert PAGE_TAG not in (by_path[path].tags or []), path
        for path in ("/settings/prompts/{task}", "/settings/prompts/{task}/reset"):
            posts = [r for r in routes if r.path == path and "POST" in r.methods]
            assert posts and all(PAGE_TAG not in (r.tags or []) for r in posts), path


# --- reading writes nothing, and the token-free pages never hold override text ------------------


def test_prompt_page_writes_nothing(tmp_path: Path) -> None:
    """``GET`` and ``HEAD`` of the list, every task page and every editor fragment (and of
    unknown tasks) change neither the folder tree, the override files (bytes, modes, times) nor
    the database, whether an override exists, is out of date or does not load."""
    with prompts_app(tmp_path) as p:
        updated = p.versioned_task()
        p.edit(TASK)
        p.edit(updated)
        p.lower(updated, p.default_version(updated) - 1)
        p.put_bytes("gap_check", b"this is not a prompt file")
        before = p.state()

        for method in ("GET", "HEAD"):
            for path in (PATH, *(task_path(task) for task in TASKS), task_path("no_such_task")):
                p.run.client.request(method, path)
        for task in TASKS:
            p.fragment(task)
            p.get(editor_path(task), htmx=True)
        for path in (editor_path("no_such_task"), editor_path("practice")):
            assert p.get(path, token=True).status_code == 404, path
        for path in (editor_path(TASK), task_path(TASK)):
            for headers in ({}, HTMX):
                p.run.client.get(path, headers=headers)  # refused or served, never written

        assert p.state() == before, "reading the prompts changed something"
        assert not (p.overrides / "assess_fit").exists(), "a read made a folder"


def test_prompt_override_text_never_reaches_a_token_free_page(tmp_path: Path) -> None:
    """Plant a distinctive marker in an override. Every token-free page (the list, every task
    page, every other area and page route), plain, with only the token and as htmx content, is
    free of it, and so is every header. The protected fragment holds it, and is refused without
    the token."""
    with prompts_app(tmp_path) as p:
        default = p.editor.default_text(TASK)
        p.editor.save_override(
            TASK,
            f"{default.system}\n{LEAK}",
            f"{default.user}\n{LEAK}",
            expected_content_version=NO_VERSION,
        )
        assert LEAK in p.file(TASK).read_text()
        paths = sorted(
            {
                route.path
                for route in p.run.app.routes
                if isinstance(route, APIRoute)
                and PAGE_TAG in (route.tags or [])
                and "GET" in route.methods
                and "{" not in route.path
            }
            | {PATH, "/settings"}
            | {task_path(task) for task in TASKS}
        )
        assert task_path(TASK) in paths and PATH in paths
        for path in paths:
            for label, response in (
                ("plain", p.run.client.get(path, follow_redirects=True)),
                ("token only", p.get(path, token=True)),
                ("htmx", p.get(path, htmx=True)),
            ):
                assert LEAK not in response.text, f"{path} ({label}) holds the override text"
                for name, value in response.headers.items():
                    assert LEAK not in value, f"{path} header {name}"
        for headers in ({}, HTMX):
            refused = p.run.client.get(editor_path(TASK), headers=headers)
            assert refused.status_code == 403, headers
            assert LEAK not in refused.text
        loaded = p.fragment(TASK)
        assert loaded.text.count(LEAK) == 2, "both sections of the override are in the fragment"
        assert "no-store" in loaded.headers.get("cache-control", "")
        assert LEAK not in str(dict(loaded.headers))


def test_prompt_text_escaped(tmp_path: Path) -> None:
    """Prompt text is data: the default's template syntax, and an override with markup,
    ``</textarea>`` and comment syntax, come out as text in the fragment (the default blocks, the
    two text areas and the compare view). No element, id or script comes from the text."""
    with prompts_app(tmp_path) as p:
        task = p.versioned_task()
        default = p.editor.default_text(task)
        none = p.fragment(task).text
        # the default's own template source is shown, not rendered
        assert one_line(block_text(none, "prompt-default-system")) == one_line(default.system)
        assert one_line(block_text(none, "prompt-default-user")) == one_line(default.user)
        shipped = p.editor.default_text(TASK).user
        assert "{{ " + UNTRUSTED_NAME + ' | untrusted("job_posting", job_id) }}' in shipped
        shell = p.fragment(TASK).text
        assert one_line(block_text(shell, "prompt-default-user")) == one_line(shipped), (
            "template syntax in the default stays literal text"
        )

        system = f"{default.system}\n{HOSTILE_LINE}\n</textarea><b id=\"injected-b\">x</b> & 'q'"
        user = f"{default.user}\n<img src=x onerror=alert(1)>\n{{% if true %}}ok{{% endif %}}"
        saved = p.editor.save_override(task, system, user, expected_content_version=NO_VERSION)
        assert saved.error is None
        p.lower(task, p.default_version(task) - 1)  # so the compare view shows it too
        for htmx in (False, True):
            response = (
                get_token(p.run, editor_path(task), htmx=True)
                if htmx
                else get_token(p.run, editor_path(task))
            )
            markup = response.text
            lowered = markup.lower()
            assert "<script" not in lowered and "<img" not in lowered, "markup stayed text"
            assert "&lt;script" in lowered and "&lt;/textarea&gt;" in lowered
            for injected in ("injected-diff", "injected-b"):
                assert by_id(markup, injected) is None, f"#{injected} came from prompt text"
            assert (
                textarea_text(markup, "prompt-system")
                == p.editor.override_text(task).system.strip()
            )
            assert textarea_text(markup, "prompt-user") == p.editor.override_text(task).user.strip()
            assert same_text(textarea_text(markup, "prompt-system"), system)
            assert same_text(textarea_text(markup, "prompt-user"), user)
            added = [
                t for kind, t in diff_lines(markup, "prompt-compare-system") if kind == "added"
            ]
            assert any("<script id=" in t for t in added), "the compare view shows it as text"
            assert [e.tag for e in elements(markup) if e.tag in ("script", "img", "b")] == []
        # hostile text kept in a refused form is escaped too
        typed = '</textarea><script id="injected">1</script>{{ ' + MARKER + " }}"
        refused = p.save(task, system=typed, version=p.status(task).content_version)
        assert refused.status_code == 422
        assert by_id(refused.text, "injected") is None and "<script" not in refused.text.lower()
        assert same_text(textarea_text(refused.text, "prompt-system"), typed)


# --- unknown tasks -----------------------------------------------------------------------------


def test_prompt_unknown_task_is_a_fixed_404_without_echo(tmp_path: Path) -> None:
    """Only the tasks of ``DEFAULT_TIERS`` have a page: another name (a prompt that exists but is
    not editable here, a wrong case, an odd string) is the same plain-text 404 on every route,
    never echoing the name, and nothing is written."""
    names = [
        "no_such_task",
        "practice",  # the fake-only practice prompt exists on disk but is not on the list
        "Summarize_job",
        "summarize_job%20",
        # Hostile markup in one path segment. (A name with a decoded "/" is not a prompt path at
        # all: no route matches it, so the app-wide unknown-path answers apply; see the test
        # after this one.)
        quote("<script>alert(1)<script>", safe=""),
        "kestrel_marker_0042",
        "x" * 200,
        "summarize-job",
    ]
    with prompts_app(tmp_path) as p:
        before = p.state()
        bodies: set[str] = set()
        for name in names:
            form = p.form(TASK)
            cases = {
                "page": p.run.client.get(f"{PATH}/{name}"),
                "page+token": p.get(f"{PATH}/{name}", token=True),
                "head": p.run.client.head(f"{PATH}/{name}"),
                "fragment": p.get(f"{PATH}/{name}/editor", token=True),
                "save": p.post(f"{PATH}/{name}", form),
                "reset": p.post(f"{PATH}/{name}/reset", {"version": NO_VERSION}),
            }
            for label, response in cases.items():
                assert response.status_code == 404, (name, label, response.status_code)
                if label == "head":
                    continue
                assert response.headers["content-type"].startswith("text/plain"), (name, label)
                for needle in {name, "script", "kestrel", "no_such", "summarize"}:
                    assert needle.lower() not in response.text.lower(), (name, label, needle)
                bodies.add(response.text)
        assert len(bodies) == 1, f"a fixed message, not one per name: {sorted(bodies)}"
        assert p.state() == before


def test_prompt_path_with_an_encoded_slash_matches_no_route_and_echoes_nothing(
    tmp_path: Path,
) -> None:
    """A task name with a decoded "/" is more than one path segment, so no prompt route matches
    it. It gets the app's answers for any unknown path (403 without the token, 404 with it), never
    an echo of the name, and nothing is written. (Corrected from the first draft of the unknown-
    task test, which expected the prompt page's own plain 404 for such a name: that 404 needs a
    matched route, and a token-free answer needs a page route, spec 2.9 rule 12.)"""
    name = quote("<script>alert(1)</script>", safe="")
    with prompts_app(tmp_path) as p:
        before = p.state()
        for path in (f"{PATH}/{name}", f"{PATH}/{name}/editor", f"{PATH}/{name}/reset"):
            for label, response in {
                "plain": p.run.client.get(path),
                "token": p.get(path, token=True),
                "post": p.post(path, {"version": NO_VERSION}),
            }.items():
                # An unmatched path is the app's own answer: 403 without the launch token,
                # 404 with it (the POST carries the token).
                wanted = 403 if label == "plain" else 404
                assert response.status_code == wanted, (path, label, response.status_code)
                assert "script" not in response.text.lower(), (path, label)
                assert "alert" not in response.text.lower(), (path, label)
        assert p.state() == before


# --- the editor fragment -----------------------------------------------------------------------


def test_prompt_editor_fragment_shows_default_override_status_and_forms(tmp_path: Path) -> None:
    """The three states of a prompt: the default alone ("Using the default v3"), an edit on the
    current default ("Edited, based on v3") and an edit made against an older default (the
    update line, the compare view). The fragment is read only, no-store, never a full page, and
    takes no focus."""
    with prompts_app(tmp_path) as p:
        task = p.versioned_task()
        default = p.default_version(task)
        text = p.editor.default_text(task)

        def check(markup: str, wanted: str, *, system: str, user: str, reset: bool) -> None:
            section = editor_section(markup)
            assert "hx-get" not in section.attrs, "the answer must not reload itself"
            assert section.attrs.get("aria-labelledby") == "prompt-editor-heading"
            assert by_known_id(markup, "prompt-editor-heading").tag == "h2"
            assert by_known_id(markup, "prompt-status").text == wanted
            assert by_known_id(markup, "prompt-status").attrs.get("tabindex") == "-1"
            assert by_id(markup, "prompt-load-error") is None
            notices = by_known_id(markup, "prompt-notices").text.lower()
            assert all(word in notices for word in NOTICE_WORDS), notices
            assert one_line(block_text(markup, "prompt-default-system")) == one_line(text.system)
            assert one_line(block_text(markup, "prompt-default-user")) == one_line(text.user)
            box = by_known_id(markup, "prompt-default")
            assert not [e for e in elements(markup) if e.tag == "form" and e.within(box)]
            assert all(
                "readonly" in e.attrs
                for e in elements(markup)
                if e.tag == "textarea" and e.within(box)
            )
            form = by_known_id(markup, "prompt-form")
            assert form.tag == "form" and form.attrs.get("method") == "post"
            assert form.attrs.get("action") == task_path(task)
            assert form.attrs.get("hx-post") == task_path(task)
            assert textarea_text(markup, "prompt-system") == system.strip()
            assert textarea_text(markup, "prompt-user") == user.strip()
            for identifier, name, word in (
                ("prompt-system", "system", "System"),
                ("prompt-user", "user", "User"),
            ):
                area = by_known_id(markup, identifier)
                assert area.attrs.get("name") == name and area.within(form)
                assert word in label_text(markup, area)
            save = by_known_id(markup, "prompt-save")
            assert save.tag == "button" and save.attrs.get("type") == "submit"
            assert save.text == "Save prompt" and save.within(form)
            version = p.status(task).content_version
            assert form_version(markup, "prompt-form") == version
            if reset:
                reset_form = by_known_id(markup, "prompt-reset-form")
                assert reset_form.attrs.get("action") == reset_path(task)
                assert reset_form.attrs.get("hx-post") == reset_path(task)
                assert form_version(markup, "prompt-reset-form") == version
                button = by_known_id(markup, "prompt-reset")
                assert button.tag == "button" and button.text == "Reset to default"
                assert button.within(reset_form)
            else:
                assert by_id(markup, "prompt-reset-form") is None
                assert by_id(markup, "prompt-reset") is None
            assert autofocus(markup) == [], "a plain load takes no focus"
            assert "<html" not in markup
            ids_are_unique(markup)

        response = p.fragment(task)
        assert "no-store" in response.headers.get("cache-control", "")
        check(
            response.text, using_default(default), system=text.system, user=text.user, reset=False
        )
        assert by_id(response.text, "prompt-compare") is None
        assert version_none(response.text)

        system, user = p.edit(task)
        response = p.fragment(task)
        check(response.text, edited_on(default), system=system, user=user, reset=True)
        assert by_id(response.text, "prompt-compare") is None
        assert not version_none(response.text)

        p.lower(task, default - 1)
        response = p.fragment(task)
        check(
            response.text,
            default_updated(default - 1, default),
            system=system,
            user=user,
            reset=True,
        )
        compare = by_known_id(response.text, "prompt-compare")
        assert "current default" in compare.text.lower(), compare.text
        # htmx and plain token-holding requests get the same fragment
        assert get_token(p.run, editor_path(task), htmx=True).text == response.text


def version_none(markup: str) -> bool:
    return form_version(markup, "prompt-form") == NO_VERSION


def test_prompt_compare_view_marks_added_and_removed_lines(tmp_path: Path) -> None:
    """When the default was updated the fragment compares the CURRENT default with the user's
    version, line by line, with a text marker on each changed line ("-" only in the default,
    "+" only in the override) and escaped text."""
    with prompts_app(tmp_path) as p:
        task = p.versioned_task()
        default = p.editor.default_text(task)
        lines = default.system.split("\n")
        first = lines[0]
        system = "\n".join([*lines[1:], HOSTILE_LINE])
        p.editor.save_override(task, system, default.user, expected_content_version=NO_VERSION)
        p.lower(task, p.default_version(task) - 1)

        markup = p.fragment(task).text
        compare = by_known_id(markup, "prompt-compare")
        assert compare.tag == "section" and compare.attrs.get("aria-labelledby")
        shown = diff_lines(markup, "prompt-compare-system")
        assert {kind for kind, _ in shown} <= {"added", "removed", "same"}
        removed = [text for kind, text in shown if kind == "removed"]
        added = [text for kind, text in shown if kind == "added"]
        assert [t[0] for t in removed] == ["-"] and one_line(first) == removed[0][1:].strip()
        assert [t[0] for t in added] == ["+"] and one_line(HOSTILE_LINE) == added[0][1:].strip()
        assert any(kind == "same" for kind, _ in shown), "unchanged lines are shown too"
        order = [kind for kind, _ in shown if kind != "same"]
        assert order == ["removed", "added"], "the removed first line comes before the added last"
        other = diff_lines(markup, "prompt-compare-user")
        assert other and {kind for kind, _ in other} == {"same"}, "the user section is unchanged"
        assert by_id(markup, "injected-diff") is None


# --- an override that does not load -------------------------------------------------------------


def hostile_bytes(valid: bytes, case: str) -> bytes:
    wrapped = b'{{ posting_context | untrusted("job_posting", job_id) }}'
    assert wrapped in valid
    if case == "unwrapped_untrusted":
        return valid.replace(wrapped, b"{{ posting_context }}")
    if case == "inputs_changed":
        return valid.replace(b"inputs: [posting_context, job_id]", b"inputs: [posting_context]")
    if case == "not_utf8":
        return b"\xff\xfe\x00 not text"
    if case == "oversize":
        return valid + b"x" * (256 * 1024)
    if case == "no_front_matter":
        return b"this is not a prompt file"
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case", ["unwrapped_untrusted", "inputs_changed", "not_utf8", "oversize", "no_front_matter"]
)
def test_prompt_override_that_does_not_load_shows_the_error_and_can_be_reset(
    tmp_path: Path, case: str
) -> None:
    """A restored or damaged override (unsafe, incompatible, not text, too big, not a prompt)
    does not crash the page: the fragment says it can't be loaded, offers Reset, and the reset
    removes it so the default applies again. The list and the shell say "Can't load"."""
    with prompts_app(tmp_path) as p:
        p.edit(TASK)
        p.put_bytes(TASK, hostile_bytes(p.file(TASK).read_bytes(), case))
        status = p.status(TASK)
        assert status.overridden and status.error

        assert BADGE_WORDS["cant-load"] in badges_in(
            p.get(PATH).text, row_of(p.get(PATH).text, TASK)
        )
        assert BADGE_WORDS["cant-load"] in badges_in(p.get(task_path(TASK)).text)
        response = p.fragment(TASK)
        markup = response.text
        error = by_known_id(markup, "prompt-load-error")
        assert error.text, "a plain message"
        assert "kestrel" not in markup.lower()
        assert by_id(markup, "prompt-compare") is None
        reset_form = by_known_id(markup, "prompt-reset-form")
        assert by_known_id(markup, "prompt-reset").within(reset_form)

        done = p.post(reset_path(TASK), {"version": form_version(markup, "prompt-reset-form")})
        assert done.status_code == 200, done.status_code
        assert said_of(done.headers) == RESET
        assert not p.file(TASK).exists()
        after = p.status(TASK)
        assert not after.overridden and after.content_version == NO_VERSION
        assert by_known_id(done.text, "prompt-status").text == using_default(after.default_version)
        assert by_id(done.text, "prompt-load-error") is None
        assert by_id(done.text, "prompt-reset-form") is None
        assert badges_in(p.get(PATH).text, row_of(p.get(PATH).text, TASK)) == []


# --- saving ------------------------------------------------------------------------------------


def test_prompt_routes_token_origin_version(tmp_path: Path) -> None:
    """Save and reset are POSTs that need the launch token and a matching origin (403 without,
    nothing changes), and the override's content version: a stale one is a 409 that changes
    nothing; the current one saves. The editor fragment needs the token."""
    with prompts_app(tmp_path) as p:
        form = p.form(TASK)
        before = p.state()
        wrong = {"origin": "http://evil.example", **p.run.with_token()}
        for path in (task_path(TASK), reset_path(TASK)):
            body = form if path == task_path(TASK) else {"version": NO_VERSION}
            for headers in ({}, p.run.with_token(), {"origin": ORIGIN}, wrong):
                refused = p.run.client.post(path, data=body, headers={**headers, **HTMX})
                assert refused.status_code == 403, (path, sorted(headers))
                refused = p.run.client.post(path, data=body, headers=headers)
                assert refused.status_code == 403, (path, sorted(headers))
        for headers in ({}, HTMX):
            assert p.run.client.get(editor_path(TASK), headers=headers).status_code == 403
        assert p.state() == before, "a refused request changes nothing"

        # a stale version: nothing is written
        stale = p.post(task_path(TASK), {**form, "version": STALE})
        assert stale.status_code == 409
        assert p.state() == before and not p.file(TASK).exists()

        # the current version saves, and then the same form is stale
        saved = p.post(task_path(TASK), form)
        assert saved.status_code == 200, saved.status_code
        assert p.status(TASK).overridden and p.file(TASK).is_file()
        after_save = p.state()
        again = p.post(task_path(TASK), form)  # still carries "none"
        assert again.status_code == 409
        assert p.state() == after_save

        # reset needs the version too
        assert p.post(reset_path(TASK), {"version": STALE}).status_code == 409
        assert p.state() == after_save and p.file(TASK).is_file()
        done = p.post(reset_path(TASK), {"version": p.status(TASK).content_version})
        assert done.status_code == 200 and not p.file(TASK).exists()


def test_prompt_save_succeeds_announces_and_keeps_the_edit(tmp_path: Path) -> None:
    """A good save writes the override (private file), answers the updated fragment with the new
    content version, a status line that takes focus and the announcement "Prompt saved."."""
    with prompts_app(tmp_path) as p:
        default = p.editor.default_text(TASK)
        system = f"{default.system}\n{EXTRA}"
        user = f"{default.user}\n{EXTRA} Again."
        response = p.save(TASK, system=system, user=user)

        assert response.status_code == 200, response.status_code
        markup = response.text
        assert "<html" not in markup
        assert said_of(response.headers) == SAVED
        status = p.status(TASK)
        assert status.overridden and status.error is None
        assert status.base_version == status.default_version
        assert (p.file(TASK).stat().st_mode & 0o777) == 0o600
        assert same_text(p.editor.override_text(TASK).system, system)
        assert same_text(p.editor.override_text(TASK).user, user)
        assert "hx-get" not in editor_section(markup).attrs
        status_line = by_known_id(markup, "prompt-status")
        assert status_line.text == edited_on(status.default_version)
        assert autofocus(markup) == [status_line], "the status line is the one focus target"
        assert status_line.attrs.get("tabindex") == "-1"
        assert form_version(markup, "prompt-form") == status.content_version
        assert form_version(markup, "prompt-reset-form") == status.content_version
        assert same_text(textarea_text(markup, "prompt-system"), system)
        assert same_text(textarea_text(markup, "prompt-user"), user)
        assert by_id(markup, "prompt-compare") is None
        listed = p.get(PATH).text
        assert badges_in(listed, row_of(listed, TASK)) == [BADGE_WORDS["edited"]]
        assert by_known_id(p.get(task_path(TASK)).text, "prompt-base-version")
        # the saved text is what a new request for the fragment shows
        assert same_text(textarea_text(p.fragment(TASK).text, "prompt-system"), system)

        # a larger prompt than the old form fields allowed still saves (about 30 KB)
        filler = "\n".join(f"Fictional filler sentence number {n} for size." for n in range(600))
        big = p.save(TASK, system=f"{default.system}\n{filler}", version=status.content_version)
        assert big.status_code == 200, big.status_code


def test_prompt_plain_forms_redirect_or_answer_the_whole_page(tmp_path: Path) -> None:
    """Without htmx a successful save or reset is 303 to the task page; a stale or refused one
    answers the whole task page with the editor in place and the same markers."""
    with prompts_app(tmp_path) as p:
        done = p.save(TASK, htmx=False)
        assert done.status_code == 303 and done.headers["location"] == task_path(TASK)
        assert p.status(TASK).overridden

        stale = p.save(TASK, htmx=False, version=STALE)
        assert stale.status_code == 409 and "<html" in stale.text
        assert by_known_id(stale.text, "page-heading").text == label_of(TASK)
        assert by_known_id(stale.text, "prompt-conflict").attrs.get("role") == "alert"

        typed = f"{{{{ {MARKER} }}}}"
        refused = p.save(TASK, htmx=False, user=typed)
        assert refused.status_code == 422 and "<html" in refused.text
        assert same_text(textarea_text(refused.text, "prompt-user"), typed)
        assert invalid_fields(refused.text)
        assert "hx-get" not in editor_section(refused.text).attrs

        gone = p.reset(TASK, htmx=False)
        assert gone.status_code == 303 and gone.headers["location"] == task_path(TASK)
        assert not p.file(TASK).exists()


@pytest.mark.parametrize("path_kind", ["save", "reset"])
def test_prompt_post_malformed_forms_are_400_and_change_nothing(
    tmp_path: Path, path_kind: str
) -> None:
    """Exactly the named fields, each once; a missing, repeated or foreign field, or a version
    that is not ``none`` or a 64-character hex string, is the plain 400 and nothing is written."""
    with prompts_app(tmp_path) as p:
        p.edit(TASK)  # something to lose
        before = p.state()
        version = str(p.status(TASK).content_version)
        text = {"system": "System text.", "user": "User text."}
        if path_kind == "save":
            path = task_path(TASK)
            good: dict[str, Any] = {"version": version, **text}
            bad_bodies: dict[str, Any] = {
                "no version": text,
                "no system": {"version": version, "user": "u"},
                "no user": {"version": version, "system": "s"},
                "empty": {},
                "foreign field": {**good, "extra": "1"},
                "only foreign": {"other": "1"},
                "repeated system": {**good, "system": ["a", "b"]},
                "repeated user": {**good, "user": ["a", "b"]},
                "repeated version": {**good, "version": [version, version]},
            }
        else:
            path = reset_path(TASK)
            good = {"version": version}
            bad_bodies = {
                "no version": {},
                "foreign field": {**good, "system": "s"},
                "only foreign": {"other": "1"},
                "repeated version": {"version": [version, version]},
            }
        for odd in ("", "x" * 300, "<script>alert(1)</script>", "../etc/passwd", "NONE"):
            bad_bodies[f"version {odd[:12]!r}"] = {**good, "version": odd}
        for name, body in bad_bodies.items():
            response = p.post(path, body)
            assert response.status_code == 400, (name, response.status_code)
            assert response.text == INVALID, (name, response.text[:80])
            assert response.headers["content-type"].startswith("text/plain"), name
            assert not has_announce(response.headers), name
        assert p.state() == before, "a malformed request changed something"


def test_prompt_save_over_the_form_cap_is_413(tmp_path: Path) -> None:
    with prompts_app(tmp_path) as p:
        before = p.state()
        response = p.post(
            task_path(TASK),
            {"version": NO_VERSION, "system": "x" * 3_000_000, "user": "y"},
        )
        assert response.status_code == 413, response.status_code
        assert p.state() == before and not p.file(TASK).exists()


def test_prompt_stale_version_is_409_before_the_text_is_looked_at(tmp_path: Path) -> None:
    """The content version is checked first: a stale version with an unsafe text is a 409 (not a
    422), nothing is written, and the answer is the reload prompt that takes focus."""
    with prompts_app(tmp_path) as p:
        p.edit(TASK)  # the page the user has open still carries "none"
        before = p.state()
        unsafe = f"{{% include '{MARKER}.md' %}}"
        response = p.post(
            task_path(TASK), {"version": NO_VERSION, "system": unsafe, "user": unsafe}
        )

        assert response.status_code == 409, response.status_code
        markup = response.text
        assert "<html" not in markup and invalid_fields(markup) == []
        prompt = by_known_id(markup, "prompt-conflict")
        assert prompt.attrs.get("role") == "alert" and "data-autofocus" in prompt.attrs
        assert VersionConflict.message in prompt.text
        links = [e for e in elements(markup) if e.tag == "a" and e.within(prompt)]
        assert [link.attrs.get("href") for link in links] == [task_path(TASK)]
        assert autofocus(markup) == [prompt]
        assert MARKER not in markup
        assert p.state() == before

        # the same for a reset whose page is stale
        reset = p.post(reset_path(TASK), {"version": NO_VERSION})
        assert reset.status_code == 409
        assert by_known_id(reset.text, "prompt-conflict").attrs.get("role") == "alert"
        assert p.file(TASK).is_file()
        assert p.state() == before


REFUSALS: dict[str, tuple[str, str]] = {
    # reason -> (where the text goes, text; {M} is the marker no message may repeat)
    "unwrapped_untrusted": ("user", "\n{{ posting_context }} {M}"),
    "include": ("user", '\n{% include "{M}.md" %}'),
    "undeclared_input": ("user", "\n{{ {M} }}"),
    "front_matter_in_body": ("user", "\n---\nid: {M}\n---\n"),
    "stray_section_header": ("system", "\n## user\n{M}"),
    "template_syntax": ("user", "\n{% if %} {M}"),
}


def refusal_form(p: Prompts, reason: str, marker: str) -> dict[str, str]:
    where, text = REFUSALS[reason]
    default = p.editor.default_text(TASK)
    form = {"system": default.system, "user": default.user}
    form[where] = form[where] + text.replace("{M}", marker)
    return form


def test_prompt_refused_template_is_422_in_place_with_a_fixed_message_per_reason(
    tmp_path: Path,
) -> None:
    """A text the safety checks refuse is a 422 that nothing is written for: the typed text
    stays in the text areas, a refused text area is ``aria-invalid`` and tied to a fixed
    message that is an alert, the first refused text area takes focus, the announcement says it
    was not saved, and no message repeats what was typed. The message differs per reason and
    does not depend on the typed text."""
    with prompts_app(tmp_path) as p:
        before = p.state()
        messages: dict[str, str] = {}
        for reason in REFUSALS:
            for marker in (MARKER, f"{MARKER}_second"):
                typed = refusal_form(p, reason, marker)
                response = p.post(task_path(TASK), {"version": NO_VERSION, **typed})
                assert response.status_code == 422, (reason, response.status_code)
                markup = response.text
                assert "<html" not in markup
                invalid = invalid_fields(markup)
                assert invalid, f"{reason}: no text area is marked invalid"
                assert {e.attrs.get("id") for e in invalid} <= {"prompt-system", "prompt-user"}
                found = {field_error(markup, field) for field in invalid}
                assert len(found) == 1, f"{reason}: one message per refusal: {found}"
                message = found.pop()
                assert message and marker not in message
                assert messages.setdefault(reason, message) == message, (
                    f"{reason}: the message depends on the typed text"
                )
                focus = autofocus(markup)
                assert focus == [invalid[0]], f"{reason}: the first refused field takes focus"
                for field in ("system", "user"):
                    assert same_text(textarea_text(markup, f"prompt-{field}"), typed[field])
                assert markup.count(marker) == sum(v.count(marker) for v in typed.values()), (
                    f"{reason}: the typed text appears only in the text areas"
                )
                assert form_version(markup, "prompt-form") == NO_VERSION
                heard = said_of(response.headers)
                assert heard.startswith(NOT_SAVED) and marker not in heard, heard
                for name, value in response.headers.items():
                    assert marker not in value, (reason, name)
        assert len(set(messages.values())) == len(REFUSALS), f"one message per reason: {messages}"
        assert p.state() == before, "a refused text writes nothing"
        assert not (p.overrides / TASK).exists()


# --- reset -------------------------------------------------------------------------------------


def test_prompt_reset_removes_the_override_and_announces(tmp_path: Path) -> None:
    with prompts_app(tmp_path) as p:
        p.edit(TASK)
        assert p.file(TASK).is_file()

        response = p.reset(TASK)

        assert response.status_code == 200, response.status_code
        markup = response.text
        assert said_of(response.headers) == RESET
        assert not p.file(TASK).exists()
        status = p.status(TASK)
        assert not status.overridden and status.content_version == NO_VERSION
        status_line = by_known_id(markup, "prompt-status")
        assert status_line.text == using_default(status.default_version)
        assert autofocus(markup) == [status_line]
        text = p.editor.default_text(TASK)
        assert same_text(textarea_text(markup, "prompt-system"), text.system)
        assert same_text(textarea_text(markup, "prompt-user"), text.user)
        assert by_id(markup, "prompt-reset-form") is None
        assert form_version(markup, "prompt-form") == NO_VERSION
        listed = p.get(PATH).text
        assert badges_in(listed, row_of(listed, TASK)) == []


def test_prompt_reset_without_an_override_is_harmless(tmp_path: Path) -> None:
    with prompts_app(tmp_path) as p:
        before = p.state()
        response = p.reset(TASK)
        assert response.status_code == 200 and said_of(response.headers) == RESET
        assert by_known_id(response.text, "prompt-status").text == using_default(
            p.default_version(TASK)
        )
        assert p.state() == before


# --- through the running app -------------------------------------------------------------------


def test_prompt_saved_through_the_route_is_used_by_the_next_task_and_reset_restores(
    tmp_path: Path,
) -> None:
    """The routes and the task runner agree: after a save through the page's own form the next
    summary runs with the override and stores ``<version>+override``; after the reset it runs
    with the default again."""
    from tests.cv_import_harness import importing
    from tests.integration.test_job_summary import answer, seed, start

    with importing(tmp_path) as run:
        answer(run)
        client, token_headers = run.client, run.run.with_token()
        fragment = client.get(editor_path("summarize_job"), headers=token_headers)
        assert fragment.status_code == 200
        version = form_version(fragment.text, "prompt-form")
        default = run.app.state.prompt_editor.default_text("summarize_job")

        saved = client.post(
            task_path("summarize_job"),
            data={"version": version, "system": f"{default.system}\n{EXTRA}", "user": default.user},
            headers=run.run.action(**HTMX),
        )
        assert saved.status_code == 200, saved.status_code
        start(run, seed(run, POSTING_TEXTS[0]))
        default_version = run.app.state.prompt_editor.status("summarize_job").default_version
        rows = run.rows("SELECT prompt_version FROM job_summaries ORDER BY rowid")
        assert rows == [{"prompt_version": f"{default_version}+override"}]
        requests = [r for r in run.spy.requests if r.task == "summarize_job"]
        assert EXTRA in requests[0].system

        current = client.get(editor_path("summarize_job"), headers=token_headers)
        reset = client.post(
            reset_path("summarize_job"),
            data={"version": form_version(current.text, "prompt-reset-form")},
            headers=run.run.action(**HTMX),
        )
        assert reset.status_code == 200
        start(run, seed(run, POSTING_TEXTS[1]))
        rows = run.rows("SELECT prompt_version FROM job_summaries ORDER BY rowid")
        assert rows[-1] == {"prompt_version": str(default_version)}
        requests = [r for r in run.spy.requests if r.task == "summarize_job"]
        assert EXTRA not in requests[-1].system


def test_prompt_override_file_front_matter_is_the_defaults_with_the_base_version(
    tmp_path: Path,
) -> None:
    """What the route writes is the part A file: the default's front matter with the default
    version it was based on (read back here to prove the route saved through the editor)."""
    with prompts_app(tmp_path) as p:
        assert p.save(TASK).status_code == 200
        data = p.file(TASK).read_bytes()
        assert front_version(data) == p.default_version(TASK)
        assert sha(data) == p.status(TASK).content_version
        assert not [x for x in p.overrides.rglob("*") if x.is_file() and x.name != "prompt.md"]


# --- robustness: deep text, resource bombs, races and failures (M9.3 fix pass 1) ---------------

DEEP_TEXT = {
    "parentheses": "{{ " + "(" * 5000 + "1" + ")" * 5000 + " }}",
    "ifs": "{% if job_id %}" * 3000 + "x" + "{% endif %}" * 3000,
    "unary_minus": "{{ " + "-" * 5000 + "1 }}",
    "brackets": "{{ " + "[" * 5000 + "]" * 5000 + " }}",
}
BOMB_TEXT = {
    "percent_width": "{{ '%2000000000d' % 1 }}",
    "center_filter": "{{ 'a' | center(1900000000) }}",
    "doubling_sets": "{% set s = 'ab' %}" + "{% set s = s ~ s %}" * 22 + "{{ s | length }}",
    "nested_literal_loops": (
        "{% for a in [1, 2, 3] %}{% for b in [1, 2, 3] %}{% for c in [1, 2, 3] %}x"
        "{% endfor %}{% endfor %}{% endfor %}"
    ),
}


def test_every_refusal_reason_has_its_own_message() -> None:
    assert set(REFUSAL_MESSAGES) == set(get_args(RefusalReason))
    assert len(set(REFUSAL_MESSAGES.values())) == len(REFUSAL_MESSAGES)


@pytest.mark.parametrize("name", sorted(DEEP_TEXT))
def test_prompt_save_of_deeply_nested_text_is_a_422_not_a_crash(tmp_path: Path, name: str) -> None:
    with prompts_app(tmp_path) as p:
        before = p.state()
        user = f"{p.editor.default_text(TASK).user}\n{DEEP_TEXT[name]}"

        response = p.save(TASK, user=user)

        assert response.status_code == 422, response.status_code
        assert invalid_fields(response.text), "the refused text area is marked"
        assert p.state() == before


@pytest.mark.parametrize("name", sorted(BOMB_TEXT))
def test_prompt_save_of_a_resource_bomb_is_a_422_with_a_fixed_message(
    tmp_path: Path, name: str
) -> None:
    with prompts_app(tmp_path) as p:
        before = p.state()
        user = f"{p.editor.default_text(TASK).user}\n{BOMB_TEXT[name]}"

        response = p.save(TASK, user=user)

        assert response.status_code == 422, response.status_code
        invalid = invalid_fields(response.text)
        assert invalid, "the refused text areas are marked"
        (message,) = {field_error(response.text, field) for field in invalid}
        assert message == REFUSAL_MESSAGES["unsupported_expression"] != REFUSAL_FALLBACK
        assert "1900000000" not in said_of(response.headers)
        assert p.state() == before


@pytest.mark.parametrize("name", sorted(DEEP_TEXT) + sorted(BOMB_TEXT))
def test_prompt_override_file_that_cannot_be_checked_still_renders_every_page(
    tmp_path: Path, name: str
) -> None:
    """A file put straight into the overrides folder (restored, or edited by hand) with a deep
    or costly text: the list, the task page and the editor answer 200 and say "Can't load", and
    Reset removes it."""
    bomb = {**DEEP_TEXT, **BOMB_TEXT}[name]
    with prompts_app(tmp_path) as p:
        p.edit(TASK)
        data = p.file(TASK).read_bytes()
        marker = b"\n## user\n\n"
        assert marker in data
        p.put_bytes(TASK, data.replace(marker, marker + bomb.encode() + b"\n", 1))
        assert p.status(TASK).error

        listed = p.get(PATH)
        assert listed.status_code == 200
        assert BADGE_WORDS["cant-load"] in badges_in(listed.text, row_of(listed.text, TASK))
        shell = p.get(task_path(TASK))
        assert shell.status_code == 200 and BADGE_WORDS["cant-load"] in badges_in(shell.text)
        fragment = p.get(editor_path(TASK), token=True)
        assert fragment.status_code == 200
        assert by_known_id(fragment.text, "prompt-load-error").text

        done = p.post(
            reset_path(TASK), {"version": form_version(fragment.text, "prompt-reset-form")}
        )

        assert done.status_code == 200, done.status_code
        assert not p.file(TASK).exists()
        assert not p.status(TASK).overridden


def test_prompt_reset_racing_a_save_is_a_409_and_keeps_the_newer_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with prompts_app(tmp_path) as p:
        p.edit(TASK)
        version = str(p.status(TASK).content_version)
        real = p.editor.reset_override
        raced: list[bool] = []

        def save_then_reset(task: str, **kwargs: Any) -> None:
            if not raced:  # another tab saves just before the reset reaches the files
                raced.append(True)
                text = p.editor.default_text(task)
                p.editor.save_override(
                    task,
                    f"{text.system}\nA fictional line from another tab.",
                    text.user,
                    expected_content_version=version,
                )
            real(task, **kwargs)

        monkeypatch.setattr(p.editor, "reset_override", save_then_reset)

        response = p.post(reset_path(TASK), {"version": version})

        assert response.status_code == 409, response.status_code
        assert by_known_id(response.text, "prompt-conflict").attrs.get("role") == "alert"
        kept = p.editor.override_text(TASK)
        assert p.file(TASK).is_file()
        assert kept is not None and "another tab" in kept.system


def test_prompt_save_and_reset_that_cannot_be_done_are_a_plain_500(tmp_path: Path) -> None:
    """A regular file where the task's override folder belongs: the page's version is "none",
    the write fails, and the answer is the fixed plain-text 500 that changes nothing."""
    from app.api.prompt_forms import NOT_DONE

    with prompts_app(tmp_path) as p:
        p.overrides.mkdir(parents=True)
        (p.overrides / TASK).write_text("fictional stray file", encoding="utf-8")
        before = p.state()

        for response in (p.save(TASK), p.reset(TASK)):
            assert response.status_code == 500, response.status_code
            assert response.text == NOT_DONE
            assert response.headers["content-type"].startswith("text/plain")

        assert p.state() == before


def test_prompt_pages_are_a_fixed_404_when_the_default_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.prompt_forms import NOT_FOUND
    from app.modules.ai.prompts import PromptError

    with prompts_app(tmp_path) as p:

        def broken(task: str) -> Any:
            raise PromptError("Prompt 'fictional': no prompt.md found.")

        monkeypatch.setattr(p.editor, "status", broken)

        for response in (p.get(task_path(TASK)), p.get(editor_path(TASK), token=True)):
            assert response.status_code == 404, response.status_code
            assert response.text == NOT_FOUND
        listed = p.get(PATH)  # the list still answers, without versions or badges
        assert listed.status_code == 200
        assert len(list_rows(listed.text)) == len(TASKS)
        assert all(badges_in(listed.text, row) == [] for row in list_rows(listed.text))


@pytest.mark.parametrize(("above", "expected"), [(3, "Edited"), (1, None)])
def test_prompt_override_based_on_a_newer_default_is_plain_edited(
    tmp_path: Path, above: int, expected: str | None
) -> None:
    """A base version above the default's (a restored export from a newer app) can't be called
    "updated": no badge, no compare view, just "Edited". A base below it is "Default updated"."""
    with prompts_app(tmp_path) as p:
        task = p.versioned_task()
        default = p.default_version(task)
        p.edit(task)
        base = default + above if expected else default - above
        p.lower(task, base)

        listed = p.get(PATH).text
        shown = badges_in(listed, row_of(listed, task))
        markup = p.fragment(task).text
        if expected:
            assert shown == [BADGE_WORDS["edited"]], shown
            assert by_known_id(markup, "prompt-status").text == expected
            assert by_id(markup, "prompt-compare") is None
            assert BADGE_WORDS["default-updated"] not in badges_in(p.get(task_path(task)).text)
        else:
            assert sorted(shown) == sorted([BADGE_WORDS["edited"], BADGE_WORDS["default-updated"]])
            assert by_known_id(markup, "prompt-status").text == default_updated(base, default)
            assert by_known_id(markup, "prompt-compare") is not None
