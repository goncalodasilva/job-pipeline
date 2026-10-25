"""Runs AI tasks: prompt, tier, request, schema validation with one retry (spec 3.6; ADR-055).

Every provider call is counted on the caller's ``UsageMeter`` (spec 3.7; M2.16): reserved
before the call and completed after it, whatever the call did.

The only module that imports jsonschema. Built in wiring and injected into services as an
``AITaskRunner``.
"""

import asyncio
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Final, get_args

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError, relevance

from app.domain.errors import InvalidModelOutput
from app.domain.settings import AIProviderSettings, Tier
from app.domain.tiers import resolve_model, resolve_tier
from app.modules.ai import (
    AIProvider,
    AIRequest,
    AIResponse,
    CallCancelled,
    CancelToken,
    TaskResult,
    TokenUsage,
    UsageMeter,
)
from app.modules.ai.metering import CallMeter
from app.modules.ai.prompts import HONESTY_NOTICE, PromptError, PromptLibrary, render_prompt
from app.modules.ai.tasks import TASK_SPECS, TaskSpec

_HONESTY_KINDS: Final = frozenset({"generation", "feedback"})
_PROBLEMS_LIMIT: Final = 500
_MAX_PROBLEMS: Final = 3
_CORRECTION: Final = (
    "A previous attempt at this task returned JSON that broke the required schema: "
    "{problems}. Produce the whole response again, as JSON only, matching the schema exactly."
)
_NOT_AN_OBJECT: Final = "$: the response was not a JSON object"
_SAFE_KEY: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SHAPE_RULES: Final = frozenset({"oneOf", "anyOf", "allOf", "if", "not"})
_LIMIT_RULES: Final[Mapping[str, str]] = {
    "minItems": "needs at least {} items",
    "maxItems": "allows at most {} items",
    "minLength": "must be at least {} characters long",
    "maxLength": "must be at most {} characters long",
    "pattern": "must match the pattern {}",
    "minimum": "must be at least {}",
    "maximum": "must be at most {}",
}


@dataclass(frozen=True)
class _Attempt:
    """One provider call: a validated result, or the sanitized problems that were found."""

    data: Mapping[str, Any] | None
    model: str
    usage: TokenUsage | None
    returned: bool  # False when the provider raised InvalidModelOutput (no response)
    problems: str


class TaskExecutor:
    def __init__(
        self,
        *,
        provider: AIProvider,
        prompts: PromptLibrary,
        settings: Callable[[], AIProviderSettings],
        provider_defaults: Mapping[Tier, str],
        specs: Mapping[str, TaskSpec] = TASK_SPECS,
    ) -> None:
        for key, spec in specs.items():
            if key != spec.name:
                raise ValueError(f"Task spec registered as {key!r} is named {spec.name!r}.")
        self._provider = provider
        self._prompts = prompts
        self._settings = settings
        self._provider_defaults = provider_defaults
        self._specs = specs

    async def run_task(
        self,
        task: str,
        inputs: Mapping[str, object],
        cancel: CancelToken,
        *,
        meter: UsageMeter,
        tier: Tier | None = None,
        task_id: str | None = None,
    ) -> TaskResult:
        if tier is not None and tier not in get_args(Tier):
            raise ValueError("Unknown tier.")
        spec = self._specs.get(task)
        if spec is None:
            raise LookupError(f"Unknown AI task {task!r}.")
        template = self._prompts.load(task)
        try:
            Draft202012Validator.check_schema(template.output_schema)
        except SchemaError:
            raise PromptError(f"Prompt {task}: schema.json is not a valid JSON Schema.") from None
        validator = Draft202012Validator(template.output_schema)
        rendered = render_prompt(template, inputs)
        settings = self._settings()
        run_tier = tier if tier is not None else resolve_tier(task, template.default_tier, settings)
        request = AIRequest(
            task=task,
            prompt_id=rendered.prompt_id,
            prompt_version=rendered.prompt_version,
            system=_with_honesty_notice(rendered.system, spec.kind),
            user=rendered.user,
            output_schema=template.output_schema,
            model=resolve_model(run_tier, settings, self._provider_defaults),
            allow_web_search=spec.allow_web_search,
        )
        call = CallMeter(meter, tier=run_tier, task_type=task, task_id=task_id)
        request = replace(
            request,
            on_usage=call.on_usage,
            on_tool_use=call.on_tool_use if spec.allow_web_search else None,
        )
        attempts = [await self._attempt(request, validator, cancel, call)]
        if attempts[0].data is None:
            if cancel.cancelled:
                raise CallCancelled
            note = _CORRECTION.format(problems=attempts[0].problems)
            retry = replace(request, user=f"{request.user}\n\n{note}")
            attempts.append(await self._attempt(retry, validator, cancel, call))
        last = attempts[-1]
        if last.data is None:
            raise InvalidModelOutput(
                detail=f"task {task}: invalid output after one retry: {last.problems}"
            )
        return TaskResult(
            data=last.data,
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
            model=last.model,
            tier=run_tier,
            attempts=len(attempts),
            usage=tuple(attempt.usage for attempt in attempts if attempt.returned),
        )

    async def _attempt(
        self,
        request: AIRequest,
        validator: Draft202012Validator,
        cancel: CancelToken,
        call: CallMeter,
    ) -> _Attempt:
        """One counted provider call. A refused reservation raises before the provider is
        called; after that the record is completed whatever happens, also when this asyncio
        task is cancelled (the completion finishes before the cancellation goes on)."""
        await call.reserve()
        try:
            response = await self._provider.run(request, cancel)
        except (Exception, asyncio.CancelledError) as error:
            await call.finish(success=False, tokens=None)
            if isinstance(error, InvalidModelOutput):
                return _Attempt(None, request.model, None, returned=False, problems=_NOT_AN_OBJECT)
            raise
        try:
            attempt = _checked(response, validator)
        except Exception:  # the provider answered: the call stays counted, with its tokens
            await call.finish(success=False, tokens=response.usage)
            raise
        await call.finish(success=attempt.data is not None, tokens=response.usage)
        return attempt


def _with_honesty_notice(system: str, kind: str) -> str:
    """The system text of a request that writes about the person (generation, feedback) ends
    with the code-owned honesty rule, whatever the template or an override says (ADR-106
    item 7). The retry request copies this text, so it carries the rule exactly once too."""
    return f"{system}\n\n{HONESTY_NOTICE}" if kind in _HONESTY_KINDS else system


def _checked(response: AIResponse, validator: Draft202012Validator) -> _Attempt:
    data = _plain_copy(response.data)
    if data is None:
        return _Attempt(
            None, response.model, response.usage, returned=True, problems=_NOT_AN_OBJECT
        )
    problems = _problems(validator.iter_errors(data), _declared_names(validator.schema))
    if problems:
        return _Attempt(None, response.model, response.usage, returned=True, problems=problems)
    return _Attempt(
        MappingProxyType(data), response.model, response.usage, returned=True, problems=""
    )


def _plain_copy(data: object) -> dict[str, Any] | None:
    """A plain JSON copy (dicts, lists, str, numbers, bools, None), or None if it isn't one."""

    def to_plain(value: object) -> object:
        if isinstance(value, Mapping):
            return dict(value)
        raise TypeError("not JSON")

    try:
        copy = json.loads(json.dumps(data, default=to_plain, allow_nan=False))
    except (TypeError, ValueError):
        return None
    return copy if isinstance(copy, dict) else None


def _declared_names(schema: object) -> frozenset[str]:
    """Every property name the schema itself declares (under any ``properties``)."""
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                names.update(key for key in properties if isinstance(key, str))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(schema)
    return frozenset(names)


def _problems(errors: Iterable[ValidationError], declared: frozenset[str]) -> str:
    """Up to three distinct problems, most relevant first, within a fixed bound."""
    found: list[str] = []
    for error in sorted(errors, key=relevance, reverse=True):
        for problem in _describe(error, declared):
            if problem not in found:
                found.append(problem)
        if len(found) >= _MAX_PROBLEMS:
            break
    return "; ".join(found[:_MAX_PROBLEMS])[:_PROBLEMS_LIMIT]


def _describe(error: ValidationError, declared: frozenset[str]) -> list[str]:
    """Where the output broke which rule. Built only from the schema and from safe parts of the
    location, never from the model's output, which may echo untrusted text and would sit
    outside any untrusted block."""
    path = _location(error.absolute_path, declared)
    rule = error.validator
    value = error.validator_value
    if rule == "type":
        return [f"{path}: value is not of type {json.dumps(value, ensure_ascii=False)}"]
    if rule == "required":
        instance = error.instance
        names = value if isinstance(value, list) else []
        return [
            f"{path}: missing required property {_quoted(name)}"
            for name in names
            if isinstance(name, str) and isinstance(instance, dict) and name not in instance
        ]
    if rule == "additionalProperties" and value is False:
        return [f"{path}: remove properties the schema doesn't define"]
    if rule == "enum":
        return [f"{path}: must be one of {json.dumps(value, ensure_ascii=False)}"]
    if rule in _LIMIT_RULES:
        shown = json.dumps(value, ensure_ascii=False) if rule == "pattern" else value
        return [f"{path}: {_LIMIT_RULES[rule].format(shown)}"]
    if rule in _SHAPE_RULES:
        return [f"{path}: doesn't match any allowed shape"]
    return [f"{path}: breaks the '{rule}' rule"]


def _location(parts: Iterable[str | int], declared: frozenset[str]) -> str:
    """A JSON path keeping indices and keys the schema declares; any other key becomes
    ``[*]``, because keys under map-shaped schemas are chosen by the model."""
    location = "$"
    for part in parts:
        if isinstance(part, int) and not isinstance(part, bool):
            location += f"[{part}]"
        elif isinstance(part, str) and part in declared and _SAFE_KEY.fullmatch(part):
            location += f".{part}"
        else:
            location += "[*]"
    return location


def _quoted(name: str) -> str:
    return "'" + name.replace("'", "\\'") + "'"
