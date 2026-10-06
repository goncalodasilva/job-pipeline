"""Settings > Providers: the routes' remaining branches and focus targets (M1.21 review).

- "Test connection" refused before it runs (no key to use, or a key of the wrong shape): 422
  with the field error and the chosen kind shown back; no provider is built and the pasted
  value is never echoed.
- A body over the form limit answers 413 on both POST routes and changes nothing.
- One focus target: a full Settings page with a result, an error or a saved status doesn't
  also focus the page heading; a form-level error (no field error) is focused itself, in
  Settings and in the guide.

Every value is fictional; providers are the app-selectable fake or recorded wiring.
"""

from pathlib import Path

import pytest
from httpx import Response

from app.api.forms import MAX_FORM_BYTES
from tests.fakes.keyring_backends import MemoryKeyring
from tests.integration.test_settings_providers import (
    SETTINGS_PROVIDERS,
    SETTINGS_TEST,
    assert_section_fragment,
    kind_radios,
    settings_version,
)
from tests.integration.test_setup_provider import (
    API_KEY_KIND,
    FAKE_KIND,
    TOKEN_KIND,
    assert_credential_error,
    keychain,
    live_statuses,
    stored,
)
from tests.integration.test_setup_provider_test import FAKE_CONNECTED, recording
from tests.integration.test_setup_redirect import Element, Running, outline
from tests.integration.test_setup_reopen_banner import ALL_M1_DONE, finished_guide, started

pytestmark = pytest.mark.integration

SHORT_KEY = "FICTIONAL-short"


def focused(elements: list[Element]) -> list[str | None]:
    return [e.attrs.get("id") for e in elements if "data-autofocus" in e.attrs]


def post(run: Running, path: str, form: dict[str, str], *, htmx: bool) -> Response:
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(path, data=form, headers=headers)


# --- "Test connection" refused before it runs ------------------------------------------------


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "plain"])
@pytest.mark.parametrize("credential", ["", SHORT_KEY], ids=["empty", "bad-shape"])
def test_settings_test_refused_save_runs_no_test(
    tmp_path: Path, memory_keyring: MemoryKeyring, credential: str, htmx: bool
) -> None:
    finished_guide(tmp_path)
    factory = recording()
    with started(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        version = settings_version(run)
        response = post(
            run, SETTINGS_TEST, {"kind": TOKEN_KIND, "credential": credential}, htmx=htmx
        )
        assert_credential_error(response)
        elements = assert_section_fragment(response) if htmx else outline(response.text)
        assert "checked" in kind_radios(elements)[TOKEN_KIND].attrs, "the kind is shown back"
        assert "connection-result" not in [e.attrs.get("id") for e in elements]
        assert focused(elements) == ["credential"], "only the field is focused"
        if credential:
            assert credential not in response.text
        assert factory.calls == 0, "no test ran"
        assert settings_version(run) == version
        assert stored(run).kind == FAKE_KIND
        assert live_statuses(run) == ALL_M1_DONE
    assert keychain(memory_keyring) == {}


# --- 413 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [SETTINGS_PROVIDERS, SETTINGS_TEST], ids=["save", "test"])
def test_settings_routes_refuse_an_oversized_body(tmp_path: Path, path: str) -> None:
    finished_guide(tmp_path)
    factory = recording()
    with started(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        version = settings_version(run)
        body = {"kind": TOKEN_KIND, "credential": "x" * (MAX_FORM_BYTES + 1)}
        response = post(run, path, body, htmx=True)
        assert response.status_code == 413, response.status_code
        assert response.text == ""
        assert factory.calls == 0
        assert settings_version(run) == version
        assert stored(run).kind == FAKE_KIND


# --- one focus target ---------------------------------------------------------------------------


def test_full_settings_page_focuses_only_the_result_or_error(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        tested = post(
            run, SETTINGS_TEST, {"kind": FAKE_KIND, "credential": FAKE_CONNECTED}, htmx=False
        )
        assert tested.status_code == 200
        assert focused(outline(tested.text)) == ["connection-result"]

        refused = post(run, SETTINGS_PROVIDERS, {"kind": TOKEN_KIND, "credential": ""}, htmx=False)
        assert refused.status_code == 422
        assert focused(outline(refused.text)) == ["credential"]

        # A plain page with nothing to report focuses the heading (for htmx navigation).
        main = run.client.get("/settings", headers=run.htmx())
        assert focused(outline(main.text)) == ["page-heading"]


def test_form_level_error_is_focused_in_settings_and_the_guide(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    """Removing a key that isn't saved is a form-level error with no field error."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        for path in (SETTINGS_PROVIDERS, "/setup/provider"):
            for htmx in (True, False):
                response = post(run, path, {"remove": API_KEY_KIND}, htmx=htmx)
                assert response.status_code == 422, (path, response.status_code)
                elements = outline(response.text)
                assert focused(elements) == ["form-error"], (path, htmx, focused(elements))
        assert live_statuses(run) == ALL_M1_DONE
