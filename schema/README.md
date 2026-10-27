# Validation contracts

`fact_store.sql` is the readable DDL; `../migrations/0001_fact_store.sql` is its
byte-identical executable migration, checked in tests. SQLite supports JSON
functions, foreign keys and append-only triggers. All schema creation is idempotent.

The three JSON files use Draft 2020-12. Fact interchange uses real JSON values;
the SQL adapter serializes value, qualifiers and evidence_locator. Run
`data_foundation.facts.validate_fact` for format checks and cross-field validity
ordering (JSON Schema alone cannot compare dates). Registry validation requires a
FormatChecker. The registry fixture is a held source, not collection permission.

Operational SQL tables are run_locks, checkpoints, failure_queue and budgets.
See ../docs/data/DAEMON.md for stage contracts and lease SQL. Schema creation does
not start a daemon, fetch sources, publish or deploy.
