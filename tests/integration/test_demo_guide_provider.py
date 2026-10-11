"""The demo's setup guide, step 2: the practice provider needs nothing pasted (M2.26 review;
user decision of 2026-10-11).

In a demo workspace (``--demo`` on the folder the app chose for it; no
``JOBPIPELINE_ALLOW_FAKE_PROVIDERS``), on a folder that did not exist before:

- step 2 lists the fake ("Fake (testing only)") and, above the choices, the line "You're in
  the demo. Choose the practice provider below; it needs no account or key." (``#demo-note``);
- the fake is preselected while nothing is saved; a saved choice is the one selected;
- the form is marked ``demo-provider`` and the credential field ``credential-field``, so the
  stylesheet can hide the field, its keychain hint and the environment-variable note while
  the fake is chosen; the "uses a tiny amount of your plan or credit" sentence of the testing
  note is in ``.uses-credit`` for the same reason (the note's text is otherwise unchanged);
- Test connection with the fake and an empty field answers "Connected", saves the fake, marks
  the step done and offers Continue, which leads to the next step; Save alone saves it too;
- nothing is written to the keychain, and no ``config.toml`` is written.

Outside a demo workspace none of this applies: with the allow variable (demo on an explicit
folder, or no demo) step 2 shows no demo line, preselects nothing, carries no demo marks and
an empty field is still rejected (the existing tests pin that message).

Settings > Providers shares the form: in the demo it carries the same marks, without the
guide's line.

Also here (M2.26 review, page render reads): the answer to ``POST /setup/provider/test`` is
rendered in a worker thread, never on the event loop (rendering reads the settings).

The app builds its own wiring from the config and the process environment. Real SQLite under
``tmp_path``; the autouse in-memory keyring; ``FrozenClock``; every value is fictional.
"""

import asyncio
import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from httpx import Response

from app.config import RuntimeConfig
from app.modules.ai.providers.fake import FAKE_FAST_MODEL
from app.wiring import ALLOW_FAKES_VARIABLE
from tests.fakes.keyring_backends import MemoryKeyring
from tests.integration.test_setup_provider import (
    FAKE_KIND,
    FAKE_LABEL,
    PROVIDER,
    TOKEN_KIND,
    by_id,
    live_statuses,
    required,
    stored,
    submit,
)
from tests.integration.test_setup_provider_test import (
    TESTING_NOTE,
    assert_connection_answer,
    continue_links,
    post_test,
)
from tests.integration.test_setup_redirect import Element, Running, outline
from tests.integration.test_setup_reopen_banner import finished_guide
from tests.integration.test_setup_welcome import make_config
from tests.integration.test_setup_welcome import started as app_started
from tests.integration.test_setup_welcome import submit as submit_welcome

pytestmark = pytest.mark.integration

DEMO_NOTE = "You're in the demo. Choose the practice provider below; it needs no account or key."
NOTE_ID = "demo-note"
FORM_ID = "provider-form"
DEMO_MARK = "demo-provider"
FIELD_MARK = "credential-field"
CREDIT_MARK = "uses-credit"
CREDIT_SENTENCE = (
    "This sends one very small request, which uses a tiny amount of your plan or credit."
)
SETTINGS_PROVIDERS_TEST = "/settings/providers/test"
SETTINGS = "/settings"
BOOTSTRAP = "config.toml"


@contextmanager
def guide_at_step_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, *, allow_variable: bool = False
) -> Iterator[Running]:
    """The app on a folder that doesn't exist yet, with its own wiring and step 1 done
    through the guide. ``source``: see ``config_for``."""
    if allow_variable:
        monkeypatch.setenv(ALLOW_FAKES_VARIABLE, "1")
    else:
        monkeypatch.delenv(ALLOW_FAKES_VARIABLE, raising=False)
    data_dir = tmp_path / "fresh"
    assert not data_dir.exists()
    with app_started(config_for(tmp_path, source, data_dir)) as run:
        done = submit_welcome(run)
        assert done.status_code == 303, (done.status_code, done.text[:300])
        assert done.headers["location"] == PROVIDER
        yield run


def config_for(tmp_path: Path, source: str, data_dir: Path) -> RuntimeConfig:
    """``"demo"``: the demo on its own folder. ``"demo-explicit"``: ``--demo --data-dir``.
    ``"cli"``: ``--data-dir`` without the demo."""
    if source == "demo-explicit":
        demo = make_config(tmp_path, "demo", data_dir=data_dir)
        return dataclasses.replace(demo, data_dir_source="cli")
    return make_config(tmp_path, source, data_dir=data_dir)


def step_2(run: Running) -> list[Element]:
    response = run.client.get(PROVIDER)
    assert response.status_code == 200, (response.status_code, response.text[:300])
    return outline(response.text)


def classes(element: Element) -> list[str]:
    return (element.attrs.get("class") or "").split()


def radios(elements: list[Element]) -> dict[str, Element]:
    return {
        e.attrs.get("value") or "": e
        for e in elements
        if e.tag == "input" and e.attrs.get("name") == "kind"
    }


def checked(elements: list[Element]) -> list[str]:
    return [kind for kind, radio in radios(elements).items() if "checked" in radio.attrs]


def with_class(elements: list[Element], name: str) -> list[Element]:
    return [e for e in elements if name in classes(e)]


def position(elements: list[Element], element: Element) -> int:
    return next(index for index, found in enumerate(elements) if found is element)


def assert_demo_marks(elements: list[Element]) -> None:
    """What the stylesheet needs to hide the field, its hint, the variable note and the
    credit sentence while the fake is chosen."""
    form = required(elements, FORM_ID)
    assert DEMO_MARK in classes(form)
    (field,) = with_class(elements, FIELD_MARK)
    assert field.within(form)
    for inside in ("credential", "credential-hint"):
        assert required(elements, inside).within(field), f"#{inside} is hidden with the field"
    labels = [e for e in elements if e.tag == "label" and e.attrs.get("for") == "credential"]
    assert len(labels) == 1 and labels[0].within(field), "the label goes with the field"
    assert required(elements, f"kind-{FAKE_KIND}").within(form), "the radio the rule looks at"
    fallback = required(elements, "env-fallback")
    assert fallback.parent is form.parent, "the variable note is the form's sibling"
    assert position(elements, fallback) > position(elements, form), "and comes after it"
    assert not required(elements, "credential-status").within(field), "the status stays shown"


def assert_no_demo_marks(elements: list[Element]) -> None:
    assert by_id(elements, NOTE_ID) is None
    assert DEMO_NOTE not in " ".join(e.text for e in elements if e.tag == "p")
    assert DEMO_MARK not in classes(required(elements, FORM_ID))


# --- the demo's step 2 -------------------------------------------------------------------------


def test_demo_step_2_lists_the_fake_with_the_demo_line_above_the_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with guide_at_step_2(tmp_path, monkeypatch, "demo") as run:
        assert run.app.state.wiring.demo_workspace is True
        elements = step_2(run)

        labels = {e.attrs.get("for"): e.text for e in elements if e.tag == "label"}
        assert labels.get(f"kind-{FAKE_KIND}") == FAKE_LABEL
        note = required(elements, NOTE_ID)
        assert note.text == DEMO_NOTE
        fieldset = next(e for e in elements if e.tag == "fieldset")
        assert position(elements, note) < position(elements, fieldset), "above the choices"
        assert "role" not in note.attrs and "data-autofocus" not in note.attrs
        assert_demo_marks(elements)


def test_demo_step_2_preselects_the_fake_while_nothing_is_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with guide_at_step_2(tmp_path, monkeypatch, "demo") as run:
        assert stored(run).kind is None, "nothing is saved yet"
        elements = step_2(run)

        assert checked(elements) == [FAKE_KIND]
        assert required(elements, "credential-status").text == "The fake provider needs no key."
        assert stored(run).kind is None, "showing the page saves nothing"


def test_demo_step_2_keeps_a_saved_choice_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with guide_at_step_2(tmp_path, monkeypatch, "demo") as run:
        refused = submit(run, TOKEN_KIND, "")  # nothing pasted: refused, and shown back
        assert refused.status_code == 422, refused.status_code
        assert checked(outline(refused.text)) == [TOKEN_KIND], "what was chosen stays chosen"

        saved = submit(run, FAKE_KIND, "", htmx=True)
        assert saved.status_code == 200, saved.status_code
        assert stored(run).kind == FAKE_KIND
        assert checked(step_2(run)) == [FAKE_KIND]


def test_demo_testing_note_keeps_its_text_with_the_credit_sentence_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with guide_at_step_2(tmp_path, monkeypatch, "demo") as run:
        elements = step_2(run)

        note = required(elements, "provider-testing")
        assert note.text == TESTING_NOTE
        (sentence,) = with_class(elements, CREDIT_MARK)
        assert sentence.within(note)
        assert sentence.text == CREDIT_SENTENCE


def test_demo_fake_connects_with_an_empty_field_and_continue_leads_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring
) -> None:
    with guide_at_step_2(tmp_path, monkeypatch, "demo") as run:
        response = post_test(run, FAKE_KIND, "")

        result = assert_connection_answer(response)
        assert result.text == f"Connected ({FAKE_FAST_MODEL})"
        assert "connection-ok" in classes(result)
        assert stored(run).kind == FAKE_KIND, "the test saved the choice first"
        assert stored(run).saved_sign_ins == ()
        assert live_statuses(run).get("provider") == "done"

        (link,) = continue_links(outline(response.text))
        following = run.client.get(link.attrs["href"] or "")
        assert following.status_code == 200, following.status_code
        current = [e for e in outline(following.text) if e.attrs.get("aria-current") == "step"]
        assert len(current) == 1 and not current[0].text.startswith("AI provider")
        assert link.attrs["href"] != PROVIDER

    assert memory_keyring.items == {}, "the fake keeps nothing in any keychain"
    assert not (tmp_path / "cfg" / BOOTSTRAP).exists(), "the demo writes no settings file"


def test_demo_fake_connects_when_no_credential_field_is_sent_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with guide_at_step_2(tmp_path, monkeypatch, "demo") as run:
        result = assert_connection_answer(post_test(run, FAKE_KIND))

        assert result.text.startswith("Connected")


def test_demo_save_alone_saves_the_fake_with_an_empty_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with guide_at_step_2(tmp_path, monkeypatch, "demo") as run:
        response = submit(run, FAKE_KIND, "", htmx=True)

        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert stored(run).kind == FAKE_KIND
        assert live_statuses(run).get("provider") != "done", "done only after Connected"
        assert continue_links(outline(response.text)) == []


def test_demo_settings_providers_has_the_marks_without_the_guide_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings > Providers shares the form: the same marks and the same empty-field test,
    but the guide's line is the guide's."""
    monkeypatch.delenv(ALLOW_FAKES_VARIABLE, raising=False)
    finished_guide(tmp_path)  # the fake saved, every step done
    with app_started(config_for(tmp_path, "demo", tmp_path / "data")) as run:
        page = run.client.get(SETTINGS)
        assert page.status_code == 200, page.status_code
        elements = outline(page.text)
        assert by_id(elements, NOTE_ID) is None
        assert DEMO_NOTE not in page.text
        assert_demo_marks(elements)
        assert checked(elements) == [FAKE_KIND]

        tested = run.client.post(
            SETTINGS_PROVIDERS_TEST,
            data={"kind": FAKE_KIND, "credential": ""},
            headers=run.action(**{"HX-Request": "true"}),
        )
        assert tested.status_code == 200, (tested.status_code, tested.text[:300])
        result = required(outline(tested.text), "connection-result")
        assert result.text == f"Connected ({FAKE_FAST_MODEL})"


# --- outside a demo workspace nothing changes --------------------------------------------------


@pytest.mark.parametrize("source", ["demo-explicit", "cli"])
def test_outside_a_demo_workspace_step_2_has_no_demo_line_and_preselects_nothing(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with guide_at_step_2(tmp_path, monkeypatch, source, allow_variable=True) as run:
        assert run.app.state.wiring.demo_workspace is False
        elements = step_2(run)

        assert FAKE_KIND in radios(elements), "control: the variable offers the fake"
        assert checked(elements) == []
        assert required(elements, "credential-status").text == "Nothing chosen yet."
        assert_no_demo_marks(elements)
        assert required(elements, "provider-testing").text == TESTING_NOTE


@pytest.mark.parametrize("source", ["demo-explicit", "cli"])
def test_outside_a_demo_workspace_an_empty_field_is_still_rejected(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with guide_at_step_2(tmp_path, monkeypatch, source, allow_variable=True) as run:
        result = assert_connection_answer(post_test(run, FAKE_KIND, ""))

        assert result.text.startswith("Not connected"), result.text
        assert "connection-failed" in classes(result)
        assert live_statuses(run).get("provider") != "done"


def test_demo_on_an_explicit_folder_without_the_variable_lists_no_fake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with guide_at_step_2(tmp_path, monkeypatch, "demo-explicit") as run:
        elements = step_2(run)

        assert FAKE_KIND not in radios(elements)
        assert_no_demo_marks(elements)
        refused = post_test(run, FAKE_KIND, "")
        assert refused.status_code == 422, refused.status_code
        assert stored(run).kind is None


# --- rendering never reads on the event loop ---------------------------------------------------


def on_the_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.parametrize("credential", ["", "fake-offline-fictional-0001", None])
def test_connection_test_answer_is_rendered_off_the_event_loop(
    credential: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rendering the step reads the settings (``context()``): after the awaited test that
    must happen in a worker thread, for a result and for a refused form alike."""
    with guide_at_step_2(tmp_path, monkeypatch, "demo") as run:
        handler = run.app.state.setup_handlers["provider"]
        real_context = handler.context
        where: list[bool] = []

        def context() -> object:
            where.append(on_the_event_loop())
            return real_context()

        monkeypatch.setattr(handler, "context", context)
        kind = FAKE_KIND if credential is not None else "not-a-kind"
        response: Response = post_test(run, kind, credential)

        assert response.status_code == (200 if credential is not None else 422)
        assert where, "control: the answer was rendered through the step's context"
        assert where == [False] * len(where), "a database read ran on the event loop"
