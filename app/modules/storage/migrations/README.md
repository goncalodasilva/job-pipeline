Numbered SQL migrations, `NNNN_description.sql` (spec 2.7). Never edit a committed one; fix forward.

- Each file runs inside one transaction opened by the runner, together with its
  `schema_migrations` row. Don't write `BEGIN`, `COMMIT` or `ROLLBACK` in a migration.
- PRAGMAs that can't change inside a transaction are ignored or fail here: `PRAGMA foreign_keys=OFF`
  is silently ignored, and `journal_mode` or `VACUUM` fail. A table rebuild (SQLite's 12-step
  ALTER) needs runner support for steps outside the transaction plus `PRAGMA foreign_key_check`.
