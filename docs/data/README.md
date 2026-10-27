# Data foundation, phase 1

- [Questions and coverage](QUESTIONS.md)
- [Ontology and immutable state](ONTOLOGY.md)
- [Daemon design and collection boundary](DAEMON.md)
- [Migration rules](../../migrations/README.md)
- [Schema contracts](../../schema/README.md)

From the repository root, offline:

```sh
/workspace/venvs/hanset-df/bin/python migrations/0001_import_legacy.py --db /tmp/hanset-review/facts.sqlite3 --dry-run
/workspace/venvs/hanset-df/bin/python migrations/0001_import_legacy.py --db /tmp/hanset-review/facts.sqlite3
/workspace/venvs/hanset-df/bin/python -m pytest tests/data_foundation -q
/workspace/venvs/hanset-df/bin/python -m pytest tests -q
```

Use external storage only: `/workspace/hanset-data/raw/` and
`/workspace/hanset-data/db/hanset_facts.sqlite3`, overridable with HANSET_DATA_DIR.
The importer accepts an explicit external DB path. Dry-run rolls back rows but
may create an empty schema file. Rerunning inserts no duplicate entities/facts.

verified_fact means confirmed on an official page; source_claim means an
attributed lead; ai_inference means a model proposal; unverified means unsupported
or unknown. Unknown is “확인 중” / “Not confirmed”; over 90 days is “오래됨”.
No phase-1 command writes site data, fetches a source, publishes or deploys.
