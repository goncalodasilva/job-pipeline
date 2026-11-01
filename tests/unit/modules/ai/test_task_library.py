"""The task list, the prompt folders and the spec 3.6 table stay in step (ADR-056 follow-up).

One test per rule over every entry of ``TASK_SPECS`` and every folder of ``prompts/``: names
are one-to-one (the fake-only practice task excepted, as spec 3.6 says), web search only for
the explicit web tasks, strict schemas, and fact ids on every unit of a generation task
(spec 3.1 items 1 and 2). The schema checkers are also run on schemas that break each rule,
so a rule that has no task to bite yet (no generation task before M6) is still proven to
bite.
"""

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from app.modules.ai.prompts import PromptLibrary
from app.modules.ai.tasks import PRACTICE_SPEC, TASK_SPECS, TaskSpec

REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_PROMPTS = REPO_ROOT / "prompts"
AI_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ai"

# The task table of spec 3.6: name -> default tier. A task outside it needs the user and an ADR.
SPEC_TASK_TIERS: dict[str, str] = {
    "extract_cv": "thorough",
    "refine_fact": "fast",
    "summarize_job": "fast",
    "assess_fit": "fast",
    "gap_check": "thorough",
    "propose_angles": "thorough",
    "tailor_cv": "thorough",
    "draft_cover_letter": "thorough",
    "draft_cv_template": "thorough",
    "enrichment_question": "fast",
    "structure_answer": "fast",
    "interview_questions": "thorough",
    "interview_web_questions": "thorough",
    "answer_feedback": "thorough",
    "draft_narrative": "thorough",
    "adapt_answer": "thorough",
    "company_brief": "thorough",
    "group_removal_reasons": "fast",
}
# Spec 3.6: the only task that "uses web search".
WEB_TASKS = frozenset({"company_brief", "interview_web_questions"})
# Spec 3.1 item 1: the tasks that write in the user's name.
GENERATION_TASKS = frozenset({"tailor_cv", "draft_cover_letter", "draft_narrative", "adapt_answer"})
FACT_ID_PATTERN = "^F[0-9]+@[0-9]+$"
# M8.2 part B: a narrative card is a short list of kind-less units (text, fact_ids,
# added_details) that may be empty: a card no confirmed fact supports has no unit to write.
# Every other generation schema keeps both rules.
KINDLESS_EMPTY_OK = frozenset({"draft_narrative"})
# M8.2 part C: ``adapt_answer`` has the same kind-less units (text, fact_ids, added_details) but
# is NEVER empty: with no usable fact nothing is asked, so a model answer with no unit is invalid
# and retried (minItems stays at 1 for it, as for every task outside KINDLESS_EMPTY_OK).
KINDLESS_UNITS = KINDLESS_EMPTY_OK | frozenset({"adapt_answer"})

# Keywords whose value is one schema, a list of schemas, or a map of name -> schema.
_ONE = ("items", "contains", "not", "if", "then", "else", "propertyNames", "unevaluatedItems")
_MANY = ("allOf", "anyOf", "oneOf", "prefixItems")
_NAMED = ("properties", "$defs", "definitions", "dependentSchemas")
# Keywords that only add conditions to the object they sit in.
_CONDITIONS = frozenset({"allOf", "anyOf", "oneOf", "not", "if", "then", "else"})
# Ways to let undeclared properties in, or to hide a schema from this walk.
_LOOSE = frozenset({"patternProperties", "unevaluatedProperties", "$ref", "$dynamicRef"})


def _prompt_folders() -> list[str]:
    return sorted(path.name for path in REPO_PROMPTS.iterdir() if path.is_dir())


def _schema(task: str) -> dict[str, Any]:
    return dict(PromptLibrary(REPO_PROMPTS).load(task).output_schema)


def _subschemas(schema: dict[str, Any], path: str = "$") -> Iterator[tuple[str, str, Any]]:
    """Every direct subschema as (keyword, path, schema)."""
    for keyword in _ONE:
        if keyword in schema:
            yield keyword, f"{path}/{keyword}", schema[keyword]
    for keyword in _MANY:
        for index, child in enumerate(schema.get(keyword, [])):
            yield keyword, f"{path}/{keyword}/{index}", child
    for keyword in _NAMED:
        for name, child in schema.get(keyword, {}).items():
            yield keyword, f"{path}/{keyword}/{name}", child
    if isinstance(schema.get("additionalProperties"), dict):
        yield "additionalProperties", f"{path}/additionalProperties", schema["additionalProperties"]


def _allows_objects(schema: dict[str, Any]) -> bool:
    declared = schema.get("type")
    return declared == "object" or (isinstance(declared, list) and "object" in declared)


def open_objects(schema: Any, path: str = "$", *, inside_closed: bool = False) -> list[str]:
    """Where a schema could let an undeclared property through: every object schema must say
    ``additionalProperties: false``. A schema with ``properties`` and no ``type`` is accepted
    only as a condition (``if``, ``anyOf`` and so on) on an object that is itself closed."""
    if not isinstance(schema, dict):
        return [f"{path}: not a schema object"]
    found = [f"{path}: uses {keyword}" for keyword in sorted(_LOOSE & set(schema))]
    is_object = _allows_objects(schema)
    closed = is_object and schema.get("additionalProperties") is False
    if is_object and not closed:
        found.append(f"{path}: an object without additionalProperties false")
    if not is_object and "properties" in schema and not inside_closed:
        found.append(f"{path}: properties outside a closed object")
    untyped = not {"type", "enum", "const"} & set(schema)
    if untyped and not inside_closed:
        found.append(f"{path}: no type, so any value (an open object too) is allowed")
    for keyword, child_path, child in _subschemas(schema, path):
        condition = keyword in _CONDITIONS and (closed or inside_closed)
        found += open_objects(child, child_path, inside_closed=condition)
    return found


def units_without_fact_ids(schema: dict[str, Any], task: str | None = None) -> list[str]:
    """What stops a generation schema from forcing fact ids onto every unit (spec 3.1 item 2;
    the pattern of ``prompt-file-format.md``). Empty when the rule holds."""
    # A cover letter's schema is a list of paragraphs (no kind); every other generation
    # task returns units with a kind.
    key = "paragraphs" if "paragraphs" in schema.get("properties", {}) else "units"
    kindless = key == "paragraphs" or task in KINDLESS_UNITS
    if key not in schema.get("required", []):
        return [f"'{key}' is not required"]
    units = schema.get("properties", {}).get(key, {})
    minimum = 0 if task in KINDLESS_EMPTY_OK else 1
    if units.get("type") != "array" or units.get("minItems", 0) < minimum:
        return [f"'{key}' is not a list of at least one unit"]
    item = units.get("items", {})
    problems = []
    needed = {"text", "fact_ids"} if kindless else {"kind", "text", "fact_ids"}
    if not needed <= set(item.get("required", [])):
        problems.append(f"a unit doesn't require {', '.join(sorted(needed))}")
    kind = item.get("properties", {}).get("kind", {})
    if not kindless and kind.get("enum") != ["content", "structure"]:
        problems.append("a unit's kind is not content or structure")
    fact_ids = item.get("properties", {}).get("fact_ids", {})
    if fact_ids.get("type") != "array" or fact_ids.get("uniqueItems") is not True:
        problems.append("fact_ids is not a list of distinct ids")
    if fact_ids.get("items") != {"type": "string", "pattern": FACT_ID_PATTERN}:
        problems.append("a fact id doesn't have to look like F12@3")
    return problems


def generation_rule_problems(spec: TaskSpec, schema: dict[str, Any]) -> list[str]:
    """The fact-id rule for one task. A task spec 3.1 names as generation must be declared
    as generation (so it can't dodge the rule under another kind), and every generation
    task's schema must force fact ids."""
    problems = []
    if spec.name in GENERATION_TASKS and spec.kind != "generation":
        problems.append(f"{spec.name} must be declared with kind 'generation'")
    if spec.kind == "generation" or spec.name in GENERATION_TASKS:
        problems += [
            f"{spec.name}: {problem}" for problem in units_without_fact_ids(schema, spec.name)
        ]
    return problems


def _generation_schema() -> dict[str, Any]:
    """The generation pattern of ``prompt-file-format.md``."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["units"],
        "properties": {
            "units": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "section", "text", "fact_ids"],
                    "properties": {
                        "kind": {"enum": ["content", "structure"]},
                        "section": {"type": "string"},
                        "text": {"type": "string"},
                        "fact_ids": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string", "pattern": FACT_ID_PATTERN},
                        },
                    },
                },
            }
        },
    }


def test_task_specs_and_prompts_one_to_one() -> None:
    folders = _prompt_folders()

    assert PRACTICE_SPEC.name not in TASK_SPECS
    assert sorted([*TASK_SPECS, PRACTICE_SPEC.name]) == folders
    assert "extract_cv" in TASK_SPECS
    library = PromptLibrary(REPO_PROMPTS)
    for name, spec in TASK_SPECS.items():
        template = library.load(name)
        assert spec.name == name
        assert (template.id, template.task) == (name, name)
        assert name in SPEC_TASK_TIERS, f"{name} is not in the task table of spec 3.6"
        assert template.default_tier == SPEC_TASK_TIERS[name]
        jsonschema.Draft202012Validator.check_schema(dict(template.output_schema))
        # add-ai-task step 6: the fixtures every task has.
        for fixture in ("ok.json", "malformed.json"):
            assert (AI_FIXTURES / name / fixture).is_file(), f"{name} needs {fixture}"


def test_web_search_only_for_explicit_web_tasks() -> None:
    assert set(SPEC_TASK_TIERS) >= WEB_TASKS
    assert PRACTICE_SPEC.allow_web_search is False
    assert TASK_SPECS["extract_cv"].allow_web_search is False
    for name, spec in TASK_SPECS.items():
        assert spec.allow_web_search is (name in WEB_TASKS), name


def test_every_schema_forbids_additional_properties() -> None:
    folders = _prompt_folders()

    assert {"extract_cv", PRACTICE_SPEC.name} <= set(folders)
    for task in folders:
        schema = _schema(task)
        assert schema.get("type") == "object", task
        assert open_objects(schema) == [], task


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("additionalProperties",), True),
        (("additionalProperties",), None),
        (("properties", "units", "items", "additionalProperties"), None),
        (("properties", "units", "items", "additionalProperties"), {"type": "string"}),
        (("properties", "units", "items", "patternProperties"), {"^x": {"type": "string"}}),
        (("properties", "units", "items", "properties", "extra"), {"type": ["object", "null"]}),
        (("properties", "units", "items", "properties", "extra"), {}),
        (("properties", "units", "items", "properties", "extra"), {"$ref": "#/$defs/open"}),
        (("properties", "units", "items", "anyOf"), [{"type": "object"}]),
    ],
)
def test_open_object_checker_catches_each_way_to_open_a_schema(
    path: tuple[str, ...], replacement: object
) -> None:
    schema = _generation_schema()
    assert open_objects(schema) == []
    target: Any = schema
    for key in path[:-1]:
        target = target[key]
    if replacement is None:
        del target[path[-1]]
    else:
        target[path[-1]] = replacement

    assert open_objects(schema) != []


def test_open_object_checker_accepts_conditions_on_a_closed_object() -> None:
    schema = _generation_schema()
    item = schema["properties"]["units"]["items"]
    item["if"] = {"properties": {"kind": {"enum": ["structure"]}}}
    item["then"] = {"properties": {"fact_ids": {"type": "array", "maxItems": 0}}}
    item["anyOf"] = [{"properties": {"text": {"type": "string"}}}]

    assert open_objects(schema) == []


def test_generation_units_require_fact_ids() -> None:
    # Every registered task, by its own schema: bites when M6 adds the first generation task.
    for name, spec in TASK_SPECS.items():
        assert generation_rule_problems(spec, _schema(name)) == [], name
    assert PRACTICE_SPEC.kind != "generation"
    assert TASK_SPECS["extract_cv"].kind == "extraction", "it lists a CV; it writes nothing"
    assert set(SPEC_TASK_TIERS) >= GENERATION_TASKS

    # Until then, the rule is proven on stand-ins.
    good = _generation_schema()
    generation = TaskSpec("tailor_cv", kind="generation")
    assert generation_rule_problems(generation, good) == []
    assert generation_rule_problems(TaskSpec("some_new_task", kind="generation"), good) == []
    # A generation task can't escape under another kind, or with a schema that has no ids.
    assert generation_rule_problems(TaskSpec("tailor_cv", kind="summary"), good) != []
    assert generation_rule_problems(generation, _schema("extract_cv")) != []
    assert generation_rule_problems(TaskSpec("extract_cv", kind="generation"), good) == []


@pytest.mark.parametrize(
    "weaken",
    [
        lambda s: s["required"].remove("units"),
        lambda s: s["properties"]["units"].pop("minItems"),
        lambda s: s["properties"]["units"]["items"]["required"].remove("fact_ids"),
        lambda s: s["properties"]["units"]["items"]["required"].remove("kind"),
        lambda s: s["properties"]["units"]["items"]["properties"].pop("fact_ids"),
        lambda s: s["properties"]["units"]["items"]["properties"]["fact_ids"]["items"].pop(
            "pattern"
        ),
        lambda s: s["properties"]["units"]["items"]["properties"]["fact_ids"].pop("uniqueItems"),
        lambda s: s["properties"]["units"]["items"]["properties"]["kind"].update(
            enum=["content", "structure", "free"]
        ),
    ],
)
def test_generation_rule_catches_each_weakened_schema(weaken: Any) -> None:
    schema = copy.deepcopy(_generation_schema())
    weaken(schema)

    assert generation_rule_problems(TaskSpec("draft_cover_letter", kind="generation"), schema)


def test_schema_files_are_plain_json_objects() -> None:
    for task in _prompt_folders():
        raw = json.loads((REPO_PROMPTS / task / "schema.json").read_text(encoding="utf-8"))
        assert isinstance(raw, dict), task


def test_narrative_schema_may_be_empty_but_never_drops_fact_ids() -> None:
    """M8.2 part B: ``draft_narrative`` has kind-less units and may return none; every weakening
    that matters for honesty is still caught for it."""
    spec = TaskSpec("draft_narrative", kind="generation")
    good = _generation_schema()
    item = good["properties"]["units"]["items"]
    item["required"] = ["text", "fact_ids", "added_details"]
    item["properties"].pop("kind")
    item["properties"].pop("section")
    item["properties"]["added_details"] = {"type": "array", "items": {"type": "string"}}
    good["properties"]["units"]["minItems"] = 0

    assert generation_rule_problems(spec, good) == []
    # The same schema is not enough for any other generation task.
    assert generation_rule_problems(TaskSpec("tailor_cv", kind="generation"), good) != []
    for weaken in (
        lambda s: s["properties"]["units"]["items"]["required"].remove("fact_ids"),
        lambda s: s["properties"]["units"]["items"]["properties"].pop("fact_ids"),
        lambda s: s["properties"]["units"]["items"]["properties"]["fact_ids"].pop("uniqueItems"),
        lambda s: s["properties"]["units"]["items"]["properties"]["fact_ids"]["items"].pop(
            "pattern"
        ),
        lambda s: s["required"].remove("units"),
    ):
        broken = copy.deepcopy(good)
        weaken(broken)
        assert generation_rule_problems(spec, broken), weaken


def test_adapt_answer_schema_has_kindless_units_but_is_never_empty_and_keeps_fact_ids() -> None:
    """M8.2 part C: ``adapt_answer`` has the units of a narrative card but no exemption for an
    empty list; every weakening that matters for honesty is still caught for it."""
    spec = TaskSpec("adapt_answer", kind="generation")
    good = _generation_schema()
    item = good["properties"]["units"]["items"]
    item["required"] = ["text", "fact_ids", "added_details"]
    item["properties"].pop("kind")
    item["properties"].pop("section")
    item["properties"]["added_details"] = {"type": "array", "items": {"type": "string"}}

    assert generation_rule_problems(spec, good) == []
    assert "adapt_answer" not in KINDLESS_EMPTY_OK, "an answer with no unit is invalid"
    # The same schema is not enough for any other generation task.
    assert generation_rule_problems(TaskSpec("tailor_cv", kind="generation"), good) != []
    for weaken in (
        lambda s: s["properties"]["units"].pop("minItems"),
        lambda s: s["properties"]["units"].update(minItems=0),
        lambda s: s["properties"]["units"]["items"]["required"].remove("fact_ids"),
        lambda s: s["properties"]["units"]["items"]["properties"].pop("fact_ids"),
        lambda s: s["properties"]["units"]["items"]["properties"]["fact_ids"].pop("uniqueItems"),
        lambda s: s["properties"]["units"]["items"]["properties"]["fact_ids"]["items"].pop(
            "pattern"
        ),
        lambda s: s["required"].remove("units"),
    ):
        broken = copy.deepcopy(good)
        weaken(broken)
        assert generation_rule_problems(spec, broken), weaken
