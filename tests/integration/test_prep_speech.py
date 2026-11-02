"""The speech gate in the practice view (M8.2 part D; AC-M8.6; ADR-124 item 10). Speech is a
capability gate only: dictate and read-aloud buttons appear when the provider declares the
matching flags and do nothing; there is no speech route and no provider speech method.

Assumed interface (chosen here where the sources left it open; the implementer matches it):

- ``app/services/speech.py``: ``speech_controls(capabilities: AICapabilities) -> SpeechControls``
  with ``SpeechControls(dictate: bool, read_aloud: bool)`` (frozen). ``dictate`` is true only when
  ``capabilities.speech_to_text`` is; ``read_aloud`` only when ``capabilities.text_to_speech``
  is. No other flag matters.
- ``AnswerPracticeService.view(aid, qid).speech`` is the ``SpeechControls`` for the capabilities
  of the provider in use now (read from the provider each time, never stored).
- The practice view (``GET /applications/{aid}/prep/practice/{qid}``) renders
  ``button[data-speech=dictate]`` ("Dictate") only when ``dictate`` is true and
  ``button[data-speech=read]`` ("Read aloud") only when ``read_aloud`` is. Each is
  ``type="button"``, disabled (``disabled`` or ``aria-disabled="true"``), has no ``hx-*``,
  ``formaction`` or ``href`` attribute (it does nothing), and ``aria-describedby`` names an
  element whose text says it is "not available yet".
- No route whose path mentions speech, dictation or text-to-speech, and no member of the AI
  provider protocol, the fake provider or the Claude provider for speech beyond the
  ``AICapabilities`` flags.
"""

import re
from pathlib import Path

import pytest

from app.modules.ai import AICapabilities
from app.modules.ai.providers.claude_sdk import ClaudeSdkProvider
from app.modules.ai.providers.fake import FakeAIProvider
from tests.board_pages import elements
from tests.integration.test_inbox import get
from tests.refine_harness import refining
from tests.tailoring_pages import Tailoring

pytestmark = pytest.mark.integration

APP = Path(__file__).resolve().parents[2] / "app"
FLAGS = {
    "none": AICapabilities(),
    "other_flags_only": AICapabilities(web_search=True, reports_token_usage=True),
    "dictate_only": AICapabilities(speech_to_text=True),
    "read_only": AICapabilities(text_to_speech=True),
    "both": AICapabilities(speech_to_text=True, text_to_speech=True, reports_token_usage=True),
}
EXPECTED = {
    "none": (False, False),
    "other_flags_only": (False, False),
    "dictate_only": (True, False),
    "read_only": (False, True),
    "both": (True, True),
}


def practice_page(r, capabilities: AICapabilities):
    """The practice view of a fictional question, with the provider declaring ``capabilities``."""
    aid = Tailoring(r).ready("speech-job").id
    qid = r.run.app.state.prep.add_bank_question(
        "Describe a fictional planning task.", "behavioral"
    ).id
    r.run.spy.capabilities = capabilities
    response = get(r, f"/applications/{aid}/prep/practice/{qid}")
    assert response.status_code == 200
    return r.run.app.state.answer_practice.view(aid, qid), response.text


def speech_buttons(html: str) -> dict[str, object]:
    return {
        str(e.attrs["data-speech"]): e
        for e in elements(html)
        if e.tag == "button" and "data-speech" in e.attrs
    }


def anything_speech(html: str) -> list[str]:
    return [str(e.attrs["data-speech"]) for e in elements(html) if "data-speech" in e.attrs]


# --- the pure gate -----------------------------------------------------------------------------


@pytest.mark.parametrize("case", list(FLAGS))
def test_speech_controls_follow_the_declared_flags(case: str):
    from app.services.speech import SpeechControls, speech_controls

    controls = speech_controls(FLAGS[case])
    assert controls == SpeechControls(*EXPECTED[case])
    assert (controls.dictate, controls.read_aloud) == EXPECTED[case]


def test_speech_controls_is_a_frozen_value_and_defaults_closed():
    from app.services.speech import SpeechControls, speech_controls

    with pytest.raises(AttributeError):
        speech_controls(AICapabilities()).dictate = True  # type: ignore[misc]
    assert speech_controls(AICapabilities()) == SpeechControls(dictate=False, read_aloud=False)


# --- AC-M8.6: hidden without the capability ----------------------------------------------------


@pytest.mark.parametrize("provider", ["claude", "fake"])
def test_ac_m8_6_speech_controls_hidden_without_capability(tmp_path: Path, provider: str):
    from app.services.speech import SpeechControls, speech_controls

    declared = {
        "claude": ClaudeSdkProvider.capabilities,
        "fake": FakeAIProvider.capabilities,
    }[provider]
    assert (declared.speech_to_text, declared.text_to_speech) == (False, False)
    assert speech_controls(declared) == SpeechControls(False, False)
    with refining(tmp_path) as r:
        view, html = practice_page(r, declared)
        assert view.speech == SpeechControls(False, False)
        assert anything_speech(html) == []
        assert "Dictate" not in html and "Read aloud" not in html


def test_the_provider_in_use_by_default_shows_no_speech_controls(tmp_path: Path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("speech-job").id
        qid = r.run.app.state.prep.add_bank_question("Describe a fictional task.", "behavioral").id
        html = get(r, f"/applications/{aid}/prep/practice/{qid}").text
        assert anything_speech(html) == []
        assert r.run.app.state.answer_practice.view(aid, qid).speech.dictate is False


@pytest.mark.parametrize("case", ["dictate_only", "read_only", "both"])
def test_speech_controls_rendered_only_for_capable_provider(tmp_path: Path, case: str):
    dictate, read = EXPECTED[case]
    with refining(tmp_path) as r:
        view, html = practice_page(r, FLAGS[case])
        assert (view.speech.dictate, view.speech.read_aloud) == (dictate, read)
        buttons = speech_buttons(html)
        assert set(buttons) == {
            name for name, wanted in (("dictate", dictate), ("read", read)) if wanted
        }
        by_id = {str(e.attrs["id"]): e for e in elements(html) if "id" in e.attrs}
        for button in buttons.values():
            assert button.attrs.get("type") == "button"
            assert "disabled" in button.attrs or button.attrs.get("aria-disabled") == "true"
            assert not [
                a for a in button.attrs if a.startswith("hx-") or a in ("formaction", "href")
            ]
            hint = by_id[str(button.attrs["aria-describedby"]).split()[0]]
            assert "not available yet" in hint.text.lower()
        words = {"dictate": "Dictate", "read": "Read aloud"}
        for name, button in buttons.items():
            assert words[name] in button.text


def test_speech_buttons_follow_the_provider_each_time(tmp_path: Path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("speech-job").id
        qid = r.run.app.state.prep.add_bank_question("Describe a fictional task.", "behavioral").id
        path = f"/applications/{aid}/prep/practice/{qid}"
        r.run.spy.capabilities = FLAGS["both"]
        assert set(speech_buttons(get(r, path).text)) == {"dictate", "read"}
        r.run.spy.capabilities = FLAGS["none"]
        assert speech_buttons(get(r, path).text) == {}


def test_a_practice_view_with_speech_writes_nothing(tmp_path: Path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("speech-job").id
        qid = r.run.app.state.prep.add_bank_question("Describe a fictional task.", "behavioral").id
        r.run.spy.capabilities = FLAGS["both"]
        before = {
            t: r.run.rows(f"SELECT * FROM {t} ORDER BY 1")  # noqa: S608
            for t in ("practice_attempts", "prep_items", "tasks", "facts")
        }
        shown = get(r, f"/applications/{aid}/prep/practice/{qid}")
        assert set(speech_buttons(shown.text)) == {"dictate", "read"}
        after = {t: r.run.rows(f"SELECT * FROM {t} ORDER BY 1") for t in before}  # noqa: S608
        assert after == before


# --- no speech route, no provider speech method ------------------------------------------------

SPEECH_WORDS = re.compile(
    r"speech|dictat|tts|transcri|synthes|read-aloud|read_aloud|voice|audio", re.I
)


def test_there_is_no_speech_route(tmp_path: Path):
    with refining(tmp_path) as r:
        paths = [getattr(route, "path", "") for route in r.run.app.routes]
        assert paths, "the route table was not read"
        assert [p for p in paths if SPEECH_WORDS.search(p)] == []


@pytest.mark.parametrize("provider", [FakeAIProvider, ClaudeSdkProvider], ids=["fake", "claude"])
def test_no_provider_has_a_speech_method(provider: type):
    members = [m for m in dir(provider) if not m.startswith("__")]
    assert [m for m in members if SPEECH_WORDS.search(m) and m != "capabilities"] == []


def test_the_provider_protocol_has_no_speech_method():
    from app.modules.ai import AIProvider

    members = [m for m in dir(AIProvider) if not m.startswith("_")]
    assert [m for m in members if SPEECH_WORDS.search(m)] == []


def test_no_browser_speech_api_is_used():
    scanned = [*(APP / "templates").rglob("*.html"), *(APP / "static").rglob("*.js")]
    assert scanned
    banned = re.compile(
        r"SpeechRecognition|speechSynthesis|SpeechSynthesisUtterance|getUserMedia|MediaRecorder"
    )
    assert [str(p) for p in scanned if banned.search(p.read_text(encoding="utf-8"))] == []
