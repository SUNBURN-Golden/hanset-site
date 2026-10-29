# HANSET venue twin — representative questions

The twin exists to answer these questions with evidence. Each answer is built only from open
`fact` / `source_claim` values; any `unverified` slot makes the criterion "unknown" (never "yes").

| # | Question | Entities · attributes needed | Time rule | Verification rule |
|---|----------|------------------------------|-----------|-------------------|
| Q1 | Which saunas have a **rapid-cold plunge** and let **foreigners** in on a **day pass today**? | facility[rapid_cold_plunge].exists · usage_condition[foreigner].policy · usage_condition[access].policy · price_plan[day_pass/drop_in].amount · hours[closure].closure_rule | facts open at query time; closure rule evaluated for the date | every criterion needs a cited value; guests-only/members access = no; unknown access is not assumed open |
| Q2 | What does a **day pass** cost at branch X on a weekday vs weekend, adult vs child? | price_plan(ticket, scope, audience, duration).amount KRW | price valid on the date (effective_from ≤ d < effective_to) | CSV/directory prices are `source_claim`; ambiguous multi-price matches go to review, never a value |
| Q3 | Is branch X **open now / closed on date D** (regular hours, monthly closure, temporary closure)? | hours[regular, scope].open_hours · hours[closure].closure_rule | same-source change closes the previous period | maintenance/cleaning windows are rejected as opening hours |
| Q4 | Can I enter with a **tattoo**? (allowed / cover_required / not_allowed / conditional) | usage_condition[tattoo].policy | open value | absence of a rule ≠ allowed; stays unverified |
| Q5 | What are the **sauna and cold-plunge temperatures**, and which baths exist (dry/wet sauna, warm, hot, cold, rapid-cold, layout)? | facility[sauna|dry_sauna|wet_sauna].temperature_c / exists · facility[cold_plunge].temperature_c · facility[warm_bath|hot_bath|cold_plunge|ice_bath].exists · facility[layout].bath_list | open value; changes keep history | out-of-range temperatures flagged; no "typical" defaults |
| Q6 | What is the **minimum age** / who may enter (guests only, members, reservation)? | usage_condition[age].min_age · usage_condition[access].policy | open value | from explicit text only |
| Q7 | What **changed** at branch X since date D (price, hours, closure, facilities)? | corrections · facts with effective_to | correction history | each change links old → new fact |

Run Q1: `python3 -m hanset_twin ask --date YYYY-MM-DD`. Summary: `python3 -m hanset_twin report`.

## Extraction rules the questions rely on

- **Day-pass classification (Q1/Q2).** A curated CSV `price_krw` becomes a `day_pass` only when the
  amount itself appears in `price_note` next to a general/adult single-admission cue (정상가 1인,
  일반/일반객, 대인, 성인/adult, 종일권, 일일, 입장료/입장권/admission/entry, day pass, `day (…)`,
  1인/1 person timed session), with qualifiers `scope=weekday` (주중/평일/weekday next to the amount),
  `audience=adult|child` and `duration_hours` when stated. Hotel-guest-only rates, add-ons and
  guests-only notes become `guest_rate`; spa courses (시수/코스/treatment), memberships and packages
  stay `listed_price`. Q1 keeps requiring a `day_pass`/`drop_in` price **and** non-guest access, so
  guest-only hotels still answer "no".
- **Active-store relevance gate.** Stored HTML must be available; assets and stale feed hours,
  closures and prices are rejected. Other-domain pages must name the venue in their title.
  Evidence quotes must occur in main text or table cells after visitor reviews/comments are removed.
  Rule-derived fields must still match the current field extractor on the quote (same field, not necessarily the same value), or receive `rule_no_longer_matches`.
  Rejections remain auditable candidates; no network requests are made.
- **Baths and hours.** English bath words use whole-word boundaries and exclude identifiers.
  Explicit hot/cold bath lists preserve 온탕 and very-hot preserves 열탕; sauna (dry+wet)
  preserves both 건식 and 습식. Maintenance/cleaning sentences never produce opening hours.
- **Answer hygiene.** Branches with an open `non_sauna_branch_suspect` quality issue (e.g. CrossFit
  boxes typed sauna) are excluded from Q1's candidate list and reported separately
  (`report` → `q1_excluded_branches`). Hours facts flagged `maintenance_window_as_hours` never count
  towards `open_today`. A CSV row (or active-store venue) whose name/website matches
  RUNPORT/런포트/butfit is never turned into a branch (owner-excluded venue).
- **"Not confirmed" text never yields a value.** `hanset_active.extract.fields` drops any sentence
  stating the fact is unconfirmed/unlisted/미확인/확인 안 됨 etc.; tattoo maps to
  not_allowed/cover_required/conditional exactly as stated; "hot/cold baths", "one cold",
  "cold bath(s)", 냉탕 and "cold plunge" give `cold_plunge.exists=true` while "cold shower" does not;
  nothing infers `rapid_cold_plunge` unless 급냉탕/"rapid cold plunge" is explicitly named.

## Re-extraction after a rule change

`change_detect`/`extract` only process *changed* items, so new rules never reach an existing DB by
themselves. After changing the deterministic rules (classifier or `fields.py`), run once:

```bash
python3 -m hanset_twin reextract [--twin-dir DIR] [--csv saunas.csv]
```

It re-runs extraction for `verified_csv` work items matching the supplied current CSV and, when the active store is available, every `active_store` work item through the current relevance gate, re-verifies the
re-derived candidates and applies them through the normal state machine: superseded open facts are
**closed** (`value_change`, or `evidence_removed` when the new rules no longer produce the value)
with `corrections` history — no fact row is ever deleted. Historical snapshots absent from the supplied CSV are not replayed or treated as withdrawn evidence. The extraction update is atomic and uses the current time for corrections. It is idempotent (a second run without
rule changes only moves `last_confirmed_at`), refuses to run without the CSV file, and takes the
same process lock as the daemon so it cannot race `run`.
