---
name: add-ai-task
description: "Procedure for adding or changing an AI task: prompt template, output schema, task function, fake-provider fixtures, tests and honesty requirements. Use whenever work touches prompts/, app/modules/ai/tasks.py, AI fixtures, or a feature needs a new AI capability, even if the user doesn't mention prompts."
---

# Add or change an AI task

AI tasks are the only way the app talks to a model. Each has one prompt template, one output schema, a default tier, fixtures and tests (spec 3.6). Follow every step; the honesty rule depends on it.

1. **Check the contract.** The task must appear in spec 3.6 with its tier and output. If it doesn't, stop and ask the user, then record an ADR.
2. **Write the prompt and schema** in `prompts/<task>/prompt.md` and `prompts/<task>/schema.json`, following `references/prompt-file-format.md`. Changing an existing prompt or schema means bumping `version`.
3. **Mark untrusted inputs.** Job text, fetched pages, web results, CV text and free-text answers go through the `untrusted` filter (spec 3.3); the renderer adds the fixed untrusted-content notice to the system section, so don't repeat it. Text derived from untrusted sources (for example AI-extracted job fields or unconfirmed CV drafts) is also untrusted and goes in `untrusted_inputs`.
4. **Write the task function** in `app/modules/ai/tasks.py`: typed domain inputs, render the prompt, resolve the tier's model, build an `AIRequest` (no tools unless this task is an explicit web task), call the provider, validate against the schema (one retry with the validation error), and map to a domain object carrying `prompt_id`, `prompt_version` and `model`.
5. **Honesty.**
   - Generation tasks (text that represents the user) require `fact_ids` on every content unit, and the calling service runs `app/domain/honesty.py` on the result.
   - Feedback and refinement tasks may suggest wording but must surface any claim not in the fact bank instead of adding it.
   - Facts marked "never use" must not reach the request; "generalize names" adds its instruction.
6. **Fixtures** in `tests/fixtures/ai/<task>/`: `ok.json`, `malformed.json`, and for generation tasks `adversarial_*.json` (invented employer, unknown fact id, inflated number, uncited paragraph). Add a job-posting fixture containing prompt-injection text for any task that reads job text.
7. **Tests:**
   - golden test of the rendered prompt for fixed fictional inputs (catches accidental prompt changes);
   - schema validation and the retry path;
   - request-capture tests through `FakeAIProvider`: no "never use" facts, untrusted content delimited, tools disabled;
   - adversarial outputs are flagged by the honesty checker;
   - the injection fixture doesn't change the task's behavior.
8. **Review.** Ask `prompt-engineer` to review prompt and schema, and `security-reviewer` if the task reads untrusted content in a new way.

Never put personal data, names or job criteria in prompt files; everything personal arrives through inputs.
