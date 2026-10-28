---
name: add-ai-task
description: "Procedure for adding or changing an AI task: prompt template, output schema, task function, fake-provider fixtures, tests and honesty requirements. Use whenever work touches prompts/, app/modules/ai/tasks.py, AI fixtures, or a feature needs a new AI capability, even if the user doesn't mention prompts."
---

# Add or change an AI task

AI tasks are the only way the app talks to a model. Each has one prompt template, one output schema, a default tier, fixtures and tests (spec 3.6). Follow every step; the honesty rule depends on it.

1. **Check the contract.** The task must appear in spec 3.6 with its tier and output. If it doesn't, stop and ask the user, then record an ADR.
2. **Write the prompt and schema** in `prompts/<task>/prompt.md` and `prompts/<task>/schema.json`, following `references/prompt-file-format.md`. Changing an existing prompt or schema means bumping `version`.
3. **Mark untrusted inputs.** Job text, fetched pages, web results, CV text and free-text answers go through the `untrusted` filter (spec 3.3); the renderer adds the fixed untrusted-content notice to the system section, so don't repeat it. Text derived from untrusted sources (for example AI-extracted job fields or unconfirmed CV drafts) is also untrusted and goes in `untrusted_inputs`. M6 sends each source as one block, including a list such as earlier angles, because lists of untrusted documents are not supported yet (TD-045).
4. **Register the task and write its typed wrapper.**
   - Add a `TaskSpec` to `TASK_SPECS` in `app/modules/ai/tasks.py` with the task's kind; set `allow_web_search=True` only for explicit web tasks. Kind and tools live in code, never in prompt front matter (ADR-055).
   - Write a typed wrapper (in the calling service or a small module next to it) that maps domain inputs to the prompt's declared inputs, calls the injected `AITaskRunner.run_task`, and maps `result.data` to a domain object carrying `prompt_id`, `prompt_version` and `model` (plus `usage` and `attempts` where budgets need them). Services never import `app.modules.ai.prompts` or `app.modules.ai.executor`.
   - The executor does the rest: it renders the prompt, resolves the tier and model, builds a no-tool `AIRequest` (unless the spec allows web search), validates against the schema, retries once with a sanitized correction note, and raises `InvalidModelOutput` after that.
   - For kinds `generation` and `feedback` the executor also appends the code-owned `HONESTY_NOTICE` as the last part of the system text, once, including on the retry (ADR-106 item 7, ADR-109). Do not repeat it in the prompt. A `summary` or `extraction` task gets no notice; `draft_cv_template` is a summary task and carries no facts or personal details (ADR-112 item 13).
   - One user start should send at most two requests (the call and its one retry). A feature whose start can send more must revisit TD-055 first.
   - Keep `tasks.input_ref` to ids and the tier. M6 stores a free-text note there for CV, letter and template drafts, a known exception (TD-283); do not copy it.
5. **Honesty.**
   - Generation tasks (text that represents the user) require `fact_ids` on every content unit, and the calling service runs `app/domain/honesty.py` on the result.
   - Generated units follow the M6 schema conventions (ADR-106 item 8, ADR-113): every unit or paragraph has `fact_ids` (`^F[0-9]+@[0-9]+$`, at most 20, unique) and an `added_details` array (at most 10 items). The checker reads `added_details` by that exact name. Text rejects control and bidi characters. A `kind: "structure"` unit may only be a template heading. No output has a status or approval field, and the schema refuses one (`additionalProperties: false`).
   - Resolve the model's output in a pure domain function before anything is shown (M6: `resolve_cv_units`, `resolve_letter_paragraphs`, `resolve_gap_check`, `resolve_angles`, `resolve_template_draft`). Code caps the counts, clears an invented role label, drops a structure unit equal to a heading, and writes any verdict wording. The model never writes the "may not be worth applying" sentence (ADR-111, ADR-113).
   - Check every citation against the facts actually sent in that request; a citation to an unsent fact is `unsupported`. Re-check the citations at approval with `approval_blockers`, which also catches facts that are now never-use, archived, discarded or superseded (ADR-106 item 8, ADR-113 item 5).
   - Feedback and refinement tasks may suggest wording but must surface unsupported claims instead of applying them. For `refine_fact`, use the target fact/role, the user's answers and the confirmed context sent with that turn (ADR-097). Save its source/version snapshot; Accept rechecks claims, the target version and context privacy. The user acknowledges each remaining claim or edits it out. A ticked claim stays in that fact's accepted wording and is recorded on the conversation; it creates no separate draft (user decision, 2026-10-19).
   - Facts marked "never use" must not reach the request; "generalize names" adds its instruction.
   - Build other-fact context only through `app.domain.fact_selection.facts_for_ai()` (confirmed, current, non-never-use). Only `fact_ref_text` and `worded_then` in the domain build fact references, and a boundary test enforces it (ADR-113 item 11). Never refine a never-use target. Delimit the target, answers and context as untrusted. Generalization controls output wording; it does not redact source text before sending. Label fake-made output and retain provenance. A user version made later inherits the practice label from its nearest ancestor that has a provider (ADR-113 item 1).
   - Personal details (name, contact details, photo) never enter a task input. The CV header and letter frame are built by code from the `personal_details` setting (ADR-106 item 3, ADR-113 item 11). Free text a user typed into a kept line or note can still carry such values (TD-286).
   - Extraction tasks such as `extract_cv` require a source quote per unit, have no status field, and save only drafts. Validate quotes and quantities; save the complete result atomically through `ctx.complete`. Follow `background-task` for paid work and meter every `run_task` with `meter`, `task_id` and the saved provider's service (ADR-080).
6. **Fixtures** in `tests/fixtures/ai/<task>/`: `ok.json`, `malformed.json`, and for generation tasks `adversarial_*.json` (invented employer, unknown fact id, inflated number, uncited paragraph). M6 CV fixtures also cover a structure claim, a wrong-role citation and a generalized name (M6.7). Add a job-posting fixture containing prompt-injection text for any task that reads job text.
7. **Tests:**
   - golden test of the rendered prompt for fixed fictional inputs (catches accidental prompt changes);
   - schema validation and the retry path;
   - request-capture tests through `FakeAIProvider`: no "never use" facts, untrusted content delimited, tools disabled; extend `tests/integration/test_m3_cross_cutting.py::test_ac_m3_4_every_ai_task_has_a_never_use_request_log_case` for every new `TASK_SPECS` entry through its real calling service;
   - adversarial outputs are flagged by the honesty checker, and approval stays blocked until each flag is resolved (edit, remove or confirm);
   - the injection fixture doesn't change the task's behavior.
8. **Review.** Ask `prompt-engineer` to review prompt and schema, and `security-reviewer` if the task reads untrusted content in a new way.

Never put personal data, names or job criteria in prompt files; everything personal arrives through inputs.
