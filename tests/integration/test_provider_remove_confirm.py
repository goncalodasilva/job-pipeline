"""Removing a saved token or API key asks first (M1.20 and M1.21 follow-up).

The guide's provider step and Settings > Providers share the fragment and the handler, so
each case runs on both POST routes (``/setup/provider`` and ``/settings/providers``):

- ``remove=<kind>`` without ``confirm`` deletes nothing. It answers 200 with the provider
  form showing an inline question ("Remove the saved token? You'd have to paste it again.",
  or "API key"), focused (the only ``data-autofocus``), with a "Remove token" (or "Remove API
  key") button that posts ``remove=<kind>`` plus ``confirm=<kind>`` (through ``hx-vals``) and
  shows its own busy note, and a "Keep it" button
  (``keep=<kind>``). No browser ``confirm()`` dialog is used.
- ``remove=<kind>`` with ``confirm=<kind>`` deletes, as before (announced for htmx, 303 for a
  plain request). A ``confirm`` that doesn't name the kind being removed is refused (422)
  and deletes nothing, as is ``keep`` sent with ``remove`` or ``action``.
- ``keep=<kind>`` deletes nothing and puts focus back on that kind's "Remove saved ..." button.
- Without the launch token or with a foreign origin, a confirmed POST is refused (403) and
  deletes nothing. The saved value is never shown.

Every value is fictional; the keyring is the in-memory test backend.
"""

import json
from pathlib import Path

import pytest
from httpx import Response

from app.domain.settings import AI_PROVIDER
from app.modules.storage import Database
from app.services.settings import SettingsService
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import MemoryKeyring
from tests.integration.test_setup_provider import (
    API_KEY_KEY,
    API_KEY_KIND,
    FICTIONAL_API_KEY,
    FICTIONAL_TOKEN,
    TOKEN_KEY,
    TOKEN_KIND,
    keychain,
    required,
    stored,
)
from tests.integration.test_setup_redirect import ORIGIN, START, Element, Running, outline
from tests.integration.test_setup_reopen_banner import finished_guide, started

pytestmark = pytest.mark.integration

GUIDE = "/setup/provider"
SETTINGS = "/settings/providers"
PATHS = [GUIDE, SETTINGS]
QUESTION = {
    TOKEN_KIND: "Remove the saved token? You'd have to paste it again.",
    API_KEY_KIND: "Remove the saved API key? You'd have to paste it again.",
}
CONFIRM_LABEL = {TOKEN_KIND: "Remove token", API_KEY_KIND: "Remove API key"}
REMOVED = {
    TOKEN_KIND: "The saved token was removed.",
    API_KEY_KIND: "The saved API key was removed.",
}
UNKNOWN_ACTION = "That choice isn't available. Reload the page and try again."
SAVED_KEYS = {TOKEN_KEY: FICTIONAL_TOKEN, API_KEY_KEY: FICTIONAL_API_KEY}


def post(run: Running, path: str, form: dict[str, str], *, htmx: bool = True) -> Response:
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(path, data=form, headers=headers)


def save_both(run: Running) -> None:
    """A saved token and a saved API key (the API key is the chosen kind)."""
    for kind, credential in ((TOKEN_KIND, FICTIONAL_TOKEN), (API_KEY_KIND, FICTIONAL_API_KEY)):
        response = post(run, SETTINGS, {"kind": kind, "credential": credential})
        assert response.status_code == 200, response.text[:300]


def version(run: Running) -> int:
    db: Database = run.app.state.workspace.db
    return SettingsService(db, FrozenClock(START)).load(AI_PROVIDER).version


def focused(elements: list[Element]) -> list[Element]:
    return [e for e in elements if "data-autofocus" in e.attrs]


def buttons(elements: list[Element], name: str) -> list[Element]:
    return [e for e in elements if e.tag == "button" and e.attrs.get("name") == name]


def assert_nothing_removed(run: Running, memory_keyring: MemoryKeyring, before: int) -> None:
    assert keychain(memory_keyring) == SAVED_KEYS
    assert set(stored(run).saved_sign_ins) == {TOKEN_KIND, API_KEY_KIND}
    assert version(run) == before, "no setting changed"


def assert_never_echoed(response: Response) -> None:
    for value in SAVED_KEYS.values():
        assert value not in response.text
        assert value not in json.dumps(dict(response.headers))


# --- the first click asks --------------------------------------------------------------------


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "plain"])
@pytest.mark.parametrize("kind", [TOKEN_KIND, API_KEY_KIND])
@pytest.mark.parametrize("path", PATHS, ids=["guide", "settings"])
def test_remove_without_confirm_asks_and_deletes_nothing(
    tmp_path: Path, memory_keyring: MemoryKeyring, path: str, kind: str, htmx: bool
) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        save_both(run)
        before = version(run)

        response = post(run, path, {"remove": kind}, htmx=htmx)

        assert response.status_code == 200, response.text[:300]
        assert "location" not in response.headers
        assert "removed" not in response.headers.get("hx-trigger", "")
        assert_nothing_removed(run, memory_keyring, before)
        assert_never_echoed(response)

        elements = outline(response.text)
        (prompt,) = focused(elements)
        assert prompt.text == QUESTION[kind]
        other = API_KEY_KIND if kind == TOKEN_KIND else TOKEN_KIND
        assert QUESTION[other] not in response.text, "only the chosen kind is asked about"

        # "Remove" posts remove=<kind> and confirm=<kind>; "Keep it" posts keep=<kind>.
        (confirm,) = [e for e in buttons(elements, "remove") if e.text == CONFIRM_LABEL[kind]]
        assert confirm.attrs.get("value") == kind
        assert json.loads(confirm.attrs.get("hx-vals") or "{}") == {"confirm": kind}
        # Its own busy note, not the save indicator.
        (indicator,) = [
            e
            for e in elements
            if e.attrs.get("id") == (confirm.attrs.get("hx-indicator") or "#")[1:]
        ]
        assert indicator.text == "Removing it from your system keychain…"
        (keep,) = buttons(elements, "keep")
        assert (keep.text, keep.attrs.get("value")) == ("Keep it", kind)
        assert "confirm(" not in response.text, "no browser dialog"
        # The other kind's "Remove saved ..." button is still there, unchanged.
        assert [e.attrs.get("value") for e in buttons(elements, "remove")] in (
            [kind, other],
            [other, kind],
        )


# --- confirmed: deleted ----------------------------------------------------------------------


@pytest.mark.parametrize("kind", [TOKEN_KIND, API_KEY_KIND])
@pytest.mark.parametrize("path", PATHS, ids=["guide", "settings"])
def test_remove_with_confirm_deletes(
    tmp_path: Path, memory_keyring: MemoryKeyring, path: str, kind: str
) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        save_both(run)

        response = post(run, path, {"remove": kind, "confirm": kind})

        assert response.status_code == 200, response.text[:300]
        assert json.loads(response.headers["hx-trigger"])["announce"] == REMOVED[kind]
        assert_never_echoed(response)
        left = {TOKEN_KIND: API_KEY_KEY, API_KEY_KIND: TOKEN_KEY}[kind]
        assert keychain(memory_keyring) == {left: SAVED_KEYS[left]}
        assert kind not in stored(run).saved_sign_ins
        assert [e.attrs.get("value") for e in buttons(outline(response.text), "remove")] == [
            {TOKEN_KIND: API_KEY_KIND, API_KEY_KIND: TOKEN_KIND}[kind]
        ]


@pytest.mark.parametrize("path", PATHS, ids=["guide", "settings"])
def test_plain_confirmed_remove_redirects_back(
    tmp_path: Path, memory_keyring: MemoryKeyring, path: str
) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        save_both(run)
        response = post(run, path, {"remove": TOKEN_KIND, "confirm": TOKEN_KIND}, htmx=False)
        assert response.status_code == 303
        assert response.headers["location"] == ("/settings" if path == SETTINGS else GUIDE)
        assert keychain(memory_keyring) == {API_KEY_KEY: FICTIONAL_API_KEY}


@pytest.mark.parametrize(
    "confirm", ["", "yes", API_KEY_KIND, "not-a-provider"], ids=["empty", "yes", "other", "bad"]
)
@pytest.mark.parametrize("path", PATHS, ids=["guide", "settings"])
def test_confirm_must_name_the_kind_removed(
    tmp_path: Path, memory_keyring: MemoryKeyring, path: str, confirm: str
) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        save_both(run)
        before = version(run)
        response = post(run, path, {"remove": TOKEN_KIND, "confirm": confirm})
        assert response.status_code == 422, (confirm, response.status_code)
        assert required(outline(response.text), "form-error").text == UNKNOWN_ACTION
        assert "removed" not in response.headers.get("hx-trigger", "")
        assert_nothing_removed(run, memory_keyring, before)


@pytest.mark.parametrize(
    "extra",
    [{"remove": TOKEN_KIND}, {"action": "delete"}],
    ids=["keep-and-remove", "keep-and-action"],
)
@pytest.mark.parametrize("path", PATHS, ids=["guide", "settings"])
def test_keep_with_remove_or_action_is_refused(
    tmp_path: Path, memory_keyring: MemoryKeyring, path: str, extra: dict[str, str]
) -> None:
    """ "Keep it" never travels with a removal: such a submit is refused and changes nothing."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        save_both(run)
        before = version(run)
        response = post(run, path, {"keep": TOKEN_KIND, **extra})
        assert response.status_code == 422, response.text[:300]
        assert required(outline(response.text), "form-error").text == UNKNOWN_ACTION
        assert "removed" not in response.headers.get("hx-trigger", "")
        assert_nothing_removed(run, memory_keyring, before)


# --- "Keep it" -------------------------------------------------------------------------------


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "plain"])
@pytest.mark.parametrize("path", PATHS, ids=["guide", "settings"])
def test_keep_it_leaves_the_secret_and_refocuses_remove(
    tmp_path: Path, memory_keyring: MemoryKeyring, path: str, htmx: bool
) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        save_both(run)
        before = version(run)
        assert post(run, path, {"remove": TOKEN_KIND}).status_code == 200

        response = post(run, path, {"keep": TOKEN_KIND}, htmx=htmx)

        assert response.status_code == 200, response.text[:300]
        assert_nothing_removed(run, memory_keyring, before)
        assert_never_echoed(response)
        elements = outline(response.text)
        (target,) = focused(elements)
        assert target.tag == "button"
        assert (target.attrs.get("name"), target.attrs.get("value")) == ("remove", TOKEN_KIND)
        assert target.text == "Remove saved token"
        assert QUESTION[TOKEN_KIND] not in response.text, "the question is gone"
        assert buttons(elements, "keep") == []


# --- token and origin --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda run: {"origin": ORIGIN}, id="no-token"),
        pytest.param(lambda run: run.with_token(origin="http://evil.example"), id="bad-origin"),
    ],
)
@pytest.mark.parametrize("path", PATHS, ids=["guide", "settings"])
def test_confirmed_remove_needs_token_and_origin(
    tmp_path: Path, memory_keyring: MemoryKeyring, path: str, headers: object
) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        save_both(run)
        before = version(run)
        response = run.client.post(
            path,
            data={"remove": TOKEN_KIND, "confirm": TOKEN_KIND},
            headers=headers(run),  # type: ignore[operator]
        )
        assert response.status_code == 403
        assert_nothing_removed(run, memory_keyring, before)
