---
name: db-migration
description: "Procedure for any database schema change: a new forward-only SQL migration, repository updates, backup behavior and the schema-equality test. Use whenever a task adds or changes tables, columns, indexes, triggers or full-text search."
---

# Change the database schema

1. **Plan the change** against spec 2.7 and the table list in the detailed design. Every mutable table has `created_at`, `updated_at` and `version`. Foreign keys are declared; list filters and sorts get indexes.
2. **Write a new migration** `app/modules/storage/migrations/NNNN_short_name.sql` with the next number. Never edit a migration that has been committed; fix forward with a new one.
3. **Data changes** that can't be pure SQL go in a matching `NNNN_short_name.py` with an `upgrade(conn)` function, run by the migration runner in the same transaction.
4. **Full-text search:** when a searchable table changes, update its FTS5 table and triggers in the same migration.
5. **Repositories:** update queries and the domain mapping; keep the optimistic-concurrency `WHERE version = ?` on updates.
6. **Tests:**
   - the schema-equality test still passes (fresh database versus upgraded step by step);
   - a test upgrades a database seeded at the previous version with fixture rows and checks the data survived;
   - repository tests for new queries, including a version conflict.
7. **Backups:** don't touch the backup step; just confirm the upgrade test sees a backup file created.
8. **Verify in full mode** before committing.
