---
# Practice task (M2.17; ADR-076 item 2): runs only on the fake provider, to exercise the task
# runner. It generates no user-facing content and cites no facts, so it has no fact ids and
# no honesty check. Its inputs are two step numbers; nothing personal or untrusted.
id: practice
version: 1
task: practice
default_tier: fast
output_schema: schema.json
inputs: [step, steps]
untrusted_inputs: []
---

## system

You take part in a practice run that checks an app's background task system. There is nothing
to analyze and nothing to write about. Respond with JSON only, matching the provided schema.

## user

This is step {{ step }} of {{ steps }} of the practice run. Confirm the step as JSON matching
the schema, with "done" set to true.
