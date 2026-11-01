# Stage C integration interfaces

All DB arguments use Stage A SQLite connections. Clocks implement `Clock` or
`FakeClock`. Nothing publishes site data. The CLI entry point is
`python3 -m hanset_active`; global `--data-dir` and `--profile` precede commands.

## Executor

```python
Executor(fetcher, *, providers=None, max_requests=100, deadline=None)
Executor.execute(task: dict)
Executor.get(url, *, kind='body', **kwargs) -> FetchResult
Executor.process(result: FetchResult) -> str | None
Executor.install()
Executor.close()
Executor.update_gaps(venue_id, *, terminal=False)
Executor.check_structure(source_id, page_type, raw_id, content, field_ids, page)
structure_signature(content, field_ids) -> str
TaskFailure(reason, *, terminal=False, retry_after=None)
BudgetExhausted()
```

The runner installs the request guard and always restores it. The guard covers
robots, redirect hops, API calls and browser subrequests. `deadline` is monotonic
seconds. Inject providers implementing `search(query, lang, limit)` for fixtures;
production providers share `Discovery.get`. The executor records sanitized raw
blobs, extractions, labels, observations, service history and gap progress.
Successful per-URL checkpoints permit replaying a partially completed directory
without refetching earlier pages. The testing-only environment hook
`HANSET_ACTIVE_TEST_EXECUTOR_SLEEP` pauses after raw persistence for crash tests.

## Runner and locks

```python
run(fetcher, *, max_minutes=None, max_requests=None, classes=None,
    dry_run=False, providers=None, initialize=True) -> dict
recover(db, *, clock=None)
bootstrap(fetcher)
fail_task(db, task, error, *, clock=None)
RunLock(db, data_dir, run_id, *, clock=None, lease_seconds=120)
RunLock.heartbeat() -> str
AlreadyRunning()
```

`RunLock` is a context manager. `run` returns state, request count, and (except
overlap/dry-run) run ID and completed-task count. `already_running` is successful.
Classes can be a comma-separated string or iterable. `initialize=False` avoids
profile bootstrapping in tests. Only an exclusive flock owner recovers unexpired
orphan leases; `recover` by itself recovers only expired leases and due retries.
SIGTERM completes the current task. Attempt three becomes dead; its final
six-hour timestamp remains in the failure record. Freshness bootstrapping reopens
due gaps and schedules known source pages using Stage B refresh intervals.

## Venue follow-ups and robots recovery

Reverse `site_links`, `lang_counterpart`, `attachments` and `images` follow only
the venue's own registrable domains (`venues.domains` or `venues.website`). No
known own domain means no follow-ups. Portal navigation stays off the queue;
language counterparts also exclude root and bare language/home paths.
`python3 -m hanset_active prune-followups` reports rejected pending tasks as JSON
with counts by domain and discovery strategy; `--apply` marks them dead and adds
`pruned_offsite_followup` failure records without deleting rows. Each non-dry run
applies this idempotent repair before scheduling work.
Directory detail pages never take the portal's own sub-sites as the venue website,
`upsert_venue` never stores a portal domain, and each non-dry run first scrubs portal
domains from existing `venues.domains`/`website` (audited as `portal_domain_scrubbed`
in `review_queue`).

Robots requests follow at most five redirects under RFC 9309, restricted to the
same registrable domain and `/robots.txt`. Every hop uses the normal identity,
rate limit and auxiliary/run budgets; the final decision is cached under the
original origin. Cross-site/non-robots redirects, challenges and unresolved
redirect chains remain disallowed. After that cache expires, a seed domain with
a dead `robots_disallowed` trial may reopen one trial if the latest robots status
was unavailable (NULL, 5xx, 429 or a redirect). Its source returns to candidate
state for fresh evaluation. Task failure counters and `robots_unavailable_retry`
audit records cap recovery at three retries per domain. Explicit Disallow rules
and robots 401/403 never qualify; cached robots rows are left intact.

## Reports and CLI

```python
report(db) -> dict
duplicate_check(db) -> dict[str, int]
status(db) -> dict
export_service_data(db, out) -> list[str]
sanitize_output(value, key='')
cli.parser() -> argparse.ArgumentParser
cli.main(argv=None) -> int
```

`report()['duplicate_check']['total']` must be zero. Its per-table counts measure
excess rows by natural key rather than trusting indexes. `export_service_data`
rejects paths inside the checkout and preserves generated relational IDs while
sanitizing text. Reports include provenance and evidence URL examples.

## Paused connector

```python
InstagramConnector(session_state_path=None, *, clock=None)
InstagramConnector.read_profile(handle) -> None
ConnectorPaused()
```

The connector raises unless enabled, within the KST hour 13, and configured with
a session-state path. With the gate open it returns `None`: v1 contains no
network, session-file reading, or browser implementation.

## Operations drafts

`ops/RUNBOOK.md` and `ops/run_active_cron.sh` contain reviewable drafts for the
requested runbook and cron wrapper. Their final destinations outside this package
need an exception to the task's explicit allowed-file scope. The wrapper uses a
separate cron flock so it does not conflict with the runner's `run.lock`.
