"""The one way fact context is built for an AI request (M3.8; AC-M3.4; spec 3.1 item 1; ADR-010,
ADR-089 item 6): ``app/domain/fact_selection.py``. Pure; every value is fictional.

Assumed interface (chosen here where the sources left it open):

- ``FactRecord(number, version, is_current, status, privacy, content, role=None, skills=())``:
  one stored version of one fact as a service reads it (``status`` a ``FactStatus``,
  ``privacy`` a ``Privacy``, ``content`` a ``FactContent``, ``role`` a label such as
  "Operations Analyst at Example Widgets Ltd").
- ``facts_for_ai(records) -> tuple[AIFact, ...]``: only confirmed, current versions of facts
  that aren't "never use". ``AIFact`` has ``ref`` (``str(ref)`` is ``F<number>@<version>``),
  ``content``, ``role``, ``skills`` and ``generalize`` (True for "generalize names").
- ``prompt_fact_block(facts, *, also_generalize=False) -> FactPromptBlock``: the code-owned
  text every task's facts go through, with the policy ``render_prompt``
  (``app/modules/ai/prompts.py``) enforces. A request rendered with it holds
  ``GENERALIZE_INSTRUCTION`` once (in its system section) when any entry is to be
  generalized, and each such entry carries ``GENERALIZE_MARK`` between its ref and the next
  entry's ref.

The tests render through that production path (``rendered``). The module is imported
inside each test, so a missing module fails every test on its own.
"""

import importlib
import re
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.facts import FactContent, FactStatus, Privacy
from app.modules.ai.prompts import PromptTemplate, render_prompt

REPO_ROOT = Path(__file__).resolve().parents[3]
ROLE = "Operations Analyst at Example Widgets Ltd"
# The smallest prompt that shows facts, as every task's prompt does: through the block.
PROBE = PromptTemplate(
    id="fact_block_probe",
    task="fact_block_probe",
    version="1",
    default_tier="fast",
    inputs=("facts",),
    untrusted_inputs=("facts",),
    system_source="Answer about the facts.",
    user_source='{{ facts | untrusted("facts", "probe") }}',
    output_schema={},
    overridden=False,
)


def selection() -> ModuleType:
    return importlib.import_module("app.domain.fact_selection")


def rendered(facts: Sequence[Any], *, also_generalize: bool = False) -> str:
    """What a request holds for ``facts``: ``prompt_fact_block`` rendered by
    ``render_prompt``, system section then user section."""
    block = selection().prompt_fact_block(facts, also_generalize=also_generalize)
    prompt = render_prompt(PROBE, {"facts": block})
    return f"{prompt.system}\n{prompt.user}"


def record(
    number: int,
    text: str,
    *,
    version: int = 1,
    current: bool = True,
    status: FactStatus = FactStatus.CONFIRMED,
    privacy: Privacy = Privacy.NORMAL,
    role: str | None = None,
    skills: tuple[str, ...] = (),
) -> Any:
    return selection().FactRecord(
        number=number,
        version=version,
        is_current=current,
        status=status,
        privacy=privacy,
        content=FactContent(text=text),
        role=role,
        skills=skills,
    )


def refs(selected: Any) -> list[str]:
    return [str(entry.ref) for entry in selected]


def segment(block: str, ref: str) -> str:
    """The part of the rendered block from ``ref`` up to the next fact reference."""
    start = block.index(ref)
    following = re.search(r"F[1-9][0-9]*@[1-9][0-9]*", block[start + len(ref) :])
    end = start + len(ref) + following.start() if following else len(block)
    return block[start:end]


# --- what is selected --------------------------------------------------------------------------


def test_never_use_removed() -> None:
    chosen = selection().facts_for_ai(
        [
            record(1, "Cut invoice processing time by 35% across 4 warehouses.", role=ROLE),
            record(
                2, "Handled the confidential Brightwater Trust audit.", privacy=Privacy.NEVER_USE
            ),
            record(3, "Planned 60 delivery routes a week with a team of 3."),
        ]
    )

    assert refs(chosen) == ["F1@1", "F3@1"]
    block = rendered(chosen)
    assert "Brightwater" not in block
    assert "F2@" not in block
    assert "35% across 4 warehouses" in block and "60 delivery routes" in block


def test_drafts_archived_discarded_removed() -> None:
    chosen = selection().facts_for_ai(
        [
            record(1, "Draft line about the Larkspur depot.", status=FactStatus.DRAFT),
            record(2, "Archived line about the Mapleford hub.", status=FactStatus.ARCHIVED),
            record(3, "Discarded line about Corran Quay.", status=FactStatus.DISCARDED),
            record(4, "Planned 60 delivery routes a week with a team of 3."),
        ]
    )

    assert refs(chosen) == ["F4@1"]
    block = rendered(chosen)
    for marker in ("Larkspur", "Mapleford", "Corran"):
        assert marker not in block


def test_only_current_version_sent() -> None:
    chosen = selection().facts_for_ai(
        [
            record(
                12, "Trained 5 new planners on the Hollowmere system.", version=1, current=False
            ),
            record(12, "Trained new planners on the routing system.", version=2, current=False),
            record(12, "Trained 4 new planners on the routing system.", version=3),
        ]
    )

    assert refs(chosen) == ["F12@3"]
    (entry,) = chosen
    assert entry.content == FactContent(text="Trained 4 new planners on the routing system.")
    block = rendered(chosen)
    assert "Hollowmere" not in block and "F12@1" not in block and "F12@2" not in block


def test_entries_keep_wording_role_and_skills() -> None:
    story = selection().FactRecord(
        number=7,
        version=2,
        is_current=True,
        status=FactStatus.CONFIRMED,
        privacy=Privacy.NORMAL,
        content=FactContent(situation="Backlog of 3.5 days.", action="Rebuilt the queue."),
        role=ROLE,
        skills=("Excel",),
    )

    (entry,) = selection().facts_for_ai([story])

    assert str(entry.ref) == "F7@2"
    assert entry.content == story.content
    assert entry.role == ROLE
    assert tuple(entry.skills) == ("Excel",)
    assert entry.generalize is False
    block = rendered([entry])
    for part in ("F7@2", "Backlog of 3.5 days.", "Rebuilt the queue.", "Example Widgets Ltd"):
        assert part in block


def test_nothing_to_send_is_an_empty_selection() -> None:
    assert tuple(selection().facts_for_ai([])) == ()
    only_hidden = [record(1, "Handled the Brightwater Trust audit.", privacy=Privacy.NEVER_USE)]
    assert tuple(selection().facts_for_ai(only_hidden)) == ()


# --- generalize names --------------------------------------------------------------------------


def test_generalize_adds_instruction_and_flags_entry() -> None:
    module = selection()
    plain = record(1, "Planned 60 delivery routes a week with a team of 3.")
    general = record(2, "Ran the Fernhill Bank payments migration.", privacy=Privacy.GENERALIZE)
    other = record(3, "Cut invoice processing time by 35% across 4 warehouses.")

    chosen = module.facts_for_ai([plain, general, other])

    assert refs(chosen) == ["F1@1", "F2@1", "F3@1"]
    assert [entry.generalize for entry in chosen] == [False, True, False]
    block = rendered(chosen)
    instruction = module.GENERALIZE_INSTRUCTION
    assert isinstance(instruction, str) and instruction.strip()
    assert block.count(instruction) == 1, "one instruction, however many facts need it"
    mark = module.GENERALIZE_MARK
    assert isinstance(mark, str) and mark.strip()
    assert mark in segment(block, "F2@1")
    assert mark not in segment(block, "F1@1")
    assert mark not in segment(block, "F3@1")
    # The fact itself is sent; the model is told how to write about it.
    assert "Fernhill Bank" in block


def test_no_generalize_instruction_without_a_generalized_fact() -> None:
    module = selection()
    chosen = module.facts_for_ai([record(1, "Planned 60 delivery routes a week.")])

    block = rendered(chosen)

    assert module.GENERALIZE_INSTRUCTION not in block
    assert module.GENERALIZE_MARK not in block


def test_generalize_instruction_invents_no_description() -> None:
    """Its example must not itself add a detail ("regional"), and whatever the model adds
    to describe a generalized name is reported, like any other added detail."""
    instruction = selection().GENERALIZE_INSTRUCTION

    assert "regional" not in instruction and "logistics" not in instruction
    assert "only by what the facts state" in instruction
    assert "added_details" in instruction


def test_generalize_instruction_is_code_owned_not_in_any_prompt_file() -> None:
    """The partial is the only place the instruction lives, so an override can't drop it."""
    instruction = selection().GENERALIZE_INSTRUCTION
    prompts = sorted((REPO_ROOT / "prompts").glob("*/prompt.md"))

    assert prompts
    for path in prompts:
        assert instruction not in path.read_text(encoding="utf-8"), path.parent.name


# --- property: nothing hidden ever reaches the block ------------------------------------------

_HIDDEN_STATUSES = (FactStatus.DRAFT, FactStatus.ARCHIVED, FactStatus.DISCARDED)
_words = st.text(alphabet="abcdefghij KLMNOP 0123456789.,%€-\n", max_size=40)


@st.composite
def fact_records(draw: st.DrawFn) -> tuple[list[Any], set[str], set[str]]:
    """Records with a unique marker each; returns them, the markers that may be sent and
    the markers that must never be."""
    count = draw(st.integers(min_value=0, max_value=12))
    records, sendable, hidden = [], set(), set()
    for index in range(count):
        marker = f"Qz{index:03d}x"
        status = draw(st.sampled_from(list(FactStatus)))
        privacy = draw(st.sampled_from(list(Privacy)))
        current = draw(st.booleans())
        text = f"{marker} {draw(_words)}"
        records.append(
            record(
                index + 1,
                text,
                version=draw(st.integers(min_value=1, max_value=9)),
                current=current,
                status=status,
                privacy=privacy,
            )
        )
        usable = status == FactStatus.CONFIRMED and current and privacy != Privacy.NEVER_USE
        (sendable if usable else hidden).add(marker)
    return records, sendable, hidden


@settings(max_examples=150, deadline=None)
@given(fact_records())
def test_no_never_use_text_in_rendered_block(
    drawn: tuple[list[Any], set[str], set[str]],
) -> None:
    module = selection()
    records, sendable, hidden = drawn

    block = rendered(module.facts_for_ai(records))

    for marker in hidden:
        assert marker not in block
    for marker in sendable:
        assert marker in block
    never_use = [r for r in records if r.privacy == Privacy.NEVER_USE]
    for hidden_record in never_use:
        assert f"F{hidden_record.number}@{hidden_record.version}" not in block
