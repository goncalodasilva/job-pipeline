---
paths:
  - "prompts/**"
  - "app/modules/ai/**"
  - "app/domain/honesty.py"
  - "tests/fixtures/ai/**"
---

# AI and prompt rules

- Follow the `add-ai-task` skill for any task or prompt change.
- Generation output carries `fact_ids` on every content unit, and the honesty checker runs on it. Never bypass or weaken `app/domain/honesty.py`.
- Facts marked "never use" must not reach any request.
- Untrusted inputs go through the `untrusted` filter; plain tasks run with no tools.
- Bump a prompt's `version` on any change, and store `prompt_id`, `prompt_version` and `model` with every output.
- Prompts contain no personal data, names or criteria.
- Tests use `FakeAIProvider`; live calls only in `tests/live/`.
