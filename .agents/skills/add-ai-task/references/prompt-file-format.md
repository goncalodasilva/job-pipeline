# Prompt file format

Each AI task has a folder `prompts/<task>/` with two files. The loader is `app/modules/ai/prompts.py`.

## prompt.md

YAML front matter (read with `yaml.safe_load`), then two sections, `## system` and then `## user`. Each section is a Jinja2 template rendered in a sandbox with exactly the task's declared inputs.

```markdown
---
id: summarize_job
version: 1
task: summarize_job
default_tier: fast            # fast | thorough
output_schema: schema.json
inputs: [job_text, job_id, preferences, controlled_lists]
untrusted_inputs: [job_text]  # must be rendered through the untrusted filter
---

## system

You summarize job postings for a job seeker.
Respond with JSON only, matching the provided schema. Use only information present in the posting;
when a field isn't stated, use null.

## user

{{ job_text | untrusted("job_posting", job_id) }}

Preferences (for deal-breaker detection):
{{ preferences | tojson }}

Allowed values for structured fields:
{{ controlled_lists | tojson }}

Summarize the job posting above as JSON matching the schema, using only what the posting states.
```

Rules:

- All seven front matter fields are required and no others are allowed. `version` is a whole number from 1, `task` matches the folder name, `output_schema` is `schema.json`, and `untrusted_inputs` is a subset of `inputs`.
- `version` increases with any change to wording or schema. Versions are recorded as text: `"<v>"` for defaults and `"<v>+override"` for user overrides.
- The renderer adds a fixed untrusted-content notice to the start of the system section whenever the task has untrusted inputs (`UNTRUSTED_NOTICE`). Don't repeat it in the prompt.
- An untrusted input may only appear as `{{ name | untrusted(source, id) }}`, as the whole expression, in the user section. No other filter, operation, slice or condition on it, and it may not appear inside filter blocks, macros, call blocks or `{% set %}` blocks. It may sit inside `{% if %}` and `{% for %}`. It never appears in the system section. The loader rejects anything else.
- `untrusted` takes text (`str`) content only. `source` is lowercase letters and underscores; `id` is letters, digits and `_ . @ -`. It removes invisible format characters and escapes every `<`-like character as `&lt;`. Lists of untrusted documents aren't supported yet; the first task that needs them adds that (with an ADR).
- Text derived from untrusted sources (for example AI-extracted job fields or unconfirmed CV drafts) is also untrusted and goes in `untrusted_inputs`.
- Templates are checked for cost before they load, in the editor and from a file alike. Only these filters: `trim lower upper length count join default d e escape string int float tojson untrusted first last sort list unique` (`join` with a written separator of at most 64 characters). Only these methods may be called, and nothing else (no macros): `items keys values get strip lstrip rstrip lower upper title capitalize splitlines split startswith endswith count find`. `*`, `**` and `%` only between written numbers. At most 8 `{% set %}` statements per section, none reading a name that is set and none building text with `~` or `+`. A loop runs over an input (or its enclosing loop's item) and nests at most 2 deep. Text too deeply nested to check is refused.
- Templates reference only declared inputs (loop and `set` variables are fine). `{% include %}`, `{% import %}`, `{% from %}` and `{% extends %}` are not allowed. `tojson` writes readable non-ASCII text and escapes `<`, `>` and `&`.
- Order the user section as: long untrusted documents first, then trusted context (preferences, allowed values), then a closing line restating the task.
- Limits: `prompt.md` up to 256 KiB, rendered system plus user up to 2 MiB.
- No personal data, names, employers or criteria in the template; they arrive through inputs.
- Keep instructions positive and specific; explain why when a rule matters ("so the user can verify each claim").
- Generation tasks state the honesty contract in the system section: use only the provided facts, cite their ids, never add employers, titles, dates, numbers or skills that aren't in the facts. For `generation` and `feedback` kinds the executor also appends the code-owned `HONESTY_NOTICE` as the last part of the system text (ADR-109), so the prompt does not repeat it. A prompt that reads job requirements keeps its own line that they are not facts about the person (the M6 prompts do; ADR-111, ADR-115).

## schema.json

A JSON Schema (draft 2020-12) for the response. Generation tasks follow this pattern:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["units"],
  "properties": {
    "units": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["kind", "section", "text", "fact_ids"],
        "properties": {
          "kind": {"enum": ["content", "structure"]},
          "section": {"type": "string"},
          "text": {"type": "string"},
          "fact_ids": {
            "type": "array",
            "uniqueItems": true,
            "items": {"type": "string", "pattern": "^F[0-9]+@[0-9]+$"}
          }
        }
      }
    }
  }
}
```

Content units must have at least one fact id; the honesty checker enforces this after validation, because JSON Schema can't express "non-empty only when kind is content" readably.

M6 generation schemas (`prompts/tailor_cv/schema.json` is the reference; `draft_cover_letter` has paragraphs and no kind) add: `fact_ids` capped at 20 and unique; `added_details` as an array of at most 10 strings of 200 characters, under that exact name; text with no control or bidi characters; and no status or approval field, which `additionalProperties: false` refuses.

## Overrides

A user override lives at `<data folder>/prompts/overrides/<task>/prompt.md` with the same format. The loader prefers it, records `prompt_version` as `<default version>+override`, and always uses the default schema (an override's `schema.json` is ignored). The override must keep the default's `id`, `task`, `inputs` and `default_tier` (tiers move in settings), keep every default untrusted input untrusted, and pass the same checks. It must be a regular file in a real folder inside the overrides folder (no links). A broken override raises `PromptError`; there is no silent fallback to the default.
