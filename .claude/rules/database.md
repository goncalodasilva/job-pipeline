---
paths:
  - "app/modules/storage/**"
---

# Database rules

- Follow the `db-migration` skill for any schema change.
- Migrations are forward-only, numbered, and never edited after commit.
- Mutable tables have `created_at`, `updated_at` and `version`; updates check `version`.
- Write transactions are short and use `BEGIN IMMEDIATE`.
- Times are stored in UTC ISO 8601; events also store an IANA time zone.
- The schema-equality test (fresh versus upgraded) must keep passing.
