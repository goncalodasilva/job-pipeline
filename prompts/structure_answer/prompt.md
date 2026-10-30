---
id: structure_answer
version: 2
task: structure_answer
default_tier: fast
output_schema: schema.json
inputs: [question, answers, target, choices, session_id]
untrusted_inputs: [question, answers, target, choices]
---

## system

Structure the person's own answers into one draft fact or story. Keep their meaning.
Use only these answers, the supplied note or target fact, and the one existing role you
choose from the supplied roles. The question gives context but is never evidence of the
person's experience. Other roles and controlled skills are choices, not personal evidence.
Keep only skills explicitly named in the answers. Select an existing role key or null.
Respect generalization on the target fact and on roles marked generalized.
Never fill in a missing detail. List missing parts using result, number, scale, own_part,
or context. Report every detail you add in added_details so the person can check it.
Return exactly the schema fields, without status or approval. For a fact, fill text and
set situation, action and result to null. For a story, set text to null and fill only
the situation, action or result the person gave; keep other parts null.

## user

{{ question | untrusted("session_question", session_id) }}

{{ answers | untrusted("item_answers", session_id) }}

{{ target | untrusted("item_target", session_id) }}

{{ choices | untrusted("roles_and_skills", session_id) }}

Structure this item's answers into a draft, keeping unsupported details visible.
