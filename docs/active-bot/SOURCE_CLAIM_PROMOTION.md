# Source claims and internal facts

`hanset_active.collect` can assess stored evidence without collecting anything.
An exact approved venue source with persisted official-domain authority, or
two independent approved sources agreeing on a normalized value, can qualify
an internal fact. Unqualified source claims show `미확인`. Every result carries
`rights: internal_review_only` and `auto_publish_to_site: false`. Publication
requires official-source confirmation.

This feature uses only the external collector SQLite database under
`HANSET_IG_DIR`. It does not use `crawler/`, the Chrome clipper, active-bot or
twin data, site files, CSVs, or queues. It performs no network, model, browser,
caption or photo work, needs no credentials, and installs no cron.

## Local reviewed policy

Store a JSON policy outside the checkout. Limit: 512,000 bytes and 1,000
approvals. Unknown keys, malformed records, future approval dates, private
hosts, HTTP web sources, and Instagram URLs represented as web sources fail
closed. The exact
URL, venue identity, reviewed domain boundary, origin group and approval date
must all be present. Official qualification requires the recorded authorization tuple and validation of the final URL against the approved domain.

Synthetic policy example; replace the sources only after operator review:

```json
{
  "schema_version": 1,
  "approvals": [
    {
      "venue_id": "sample",
      "venue_key": "sample",
      "source_url": "https://alpha.test/hours",
      "source_kind": "web",
      "registrable_domain": "alpha.test",
      "domain_boundary_reviewed": true,
      "document_origin_group": "alpha-original",
      "approved_at": "2026-11-09"
    },
    {
      "venue_id": "sample",
      "venue_key": "sample",
      "source_url": "https://www.instagram.com/p/SamplePost/",
      "source_kind": "instagram",
      "registrable_domain": "instagram.com",
      "domain_boundary_reviewed": true,
      "document_origin_group": "venue-original-post",
      "approved_at": "2026-11-09",
      "official_handle": "sample.venue"
    }
  ]
}
```

Standard web approval means the operator checked that the exact document is
official for that `(venue_id, venue_key)`. Set the optional `review_only`
boolean to `true` for third-party web evidence such as a public blog or a
declined brand's site. This field is available on web approvals only. For both
classes the operator confirms the venue association, the registrable boundary
and the original document's origin group. Assign the **same
origin group** to mirror copies, translations, syndication and redirect
aliases, including copies hosted on different domains. Different hashes or
different short quotes do not establish independence. The program cannot
discover unrecorded copying relationships without source access; those are
the operator's review responsibility. Do not approve uncertain relationships.

The existing domain helper covers a small suffix set. Known suffix boundaries
must match that helper. Other boundaries require the explicit
`domain_boundary_reviewed: true` attestation and a hostname suffix match.
Nested domain boundaries cannot count as independent. A policy's uncertainty
must be represented by omitting its approval, never by guessing a boundary.
Duplicate canonical URL approvals are ambiguous and cannot vote, even when
their fields match. Removing an approval revokes it on the next assessment.
Approval records do not rewrite claim provenance or grant collection access.

Instagram is the explicit local-intake exception to the HTTP denial rule;
post/profile/reel URLs use the existing strict canonicalizer. `official_handle`
is mandatory and must be operator-attested: existing claim rows do not retain
the batch account handle, and post URLs cannot establish account ownership.
No account lookup occurs. An agreement pair must include a web source.

## Eligibility and comparison

Claims require `source_claim`, internal rights, resolved venue identity,
exact policy approval and the channel's existing provenance tuple:

| Channel | Origin / role / verbatim |
| --- | --- |
| Web | `web_verbatim / page_span / 1` |
| Instagram operator summary | `operator_summary / summary_span / 0` |
| Legacy NULL provenance | Reportable, ineligible |

Agreeing sources must have distinct canonical URLs, registrable domains and
reviewed origin groups, with at least one web source. Equal nonempty content
hashes veto that pair. Repeated observations, pages or subdomains within one
domain cannot manufacture agreement.

The `source-claim-v2` normalizer never changes `claims.value_json`:

| Field | Comparison |
| --- | --- |
| `hours` | Valid padded times, whitespace and range separator normalization; preserve exact scope/facility qualifiers. Overnight ranges are allowed. Equal endpoints, including midnight-to-midnight, are ambiguous; only explicit `24_hours` represents all-day hours. |
| `price_day_pass` | Integer won in the established structured shape, or explicit `KRW`/`원` strings with valid grouping; preserve ticket, scope, audience, duration, and facility. Bare legacy strings and non-KRW currency are unsupported. |
| Policy enums / rapid plunge | Valid enum strings / strict boolean. |
| Drop-in, parking, showers | Explicit Boolean availability. A quoted price alone does not establish availability. |
| Temperatures | Finite typed number or ordered two-number range; no inferred equivalence between scalar and range. |
| Bath composition | Nonempty string list, compared as a sorted set. |
| Closures | Bounded nonempty text, exact equality; no semantic inference. |

Missing qualifiers remain absent, not wildcards or inferred defaults.
Adult/child, weekday/weekend, pool/sauna and different durations stay separate.
Unsupported/ambiguous values stay visible with a diagnostic and cannot vote.
Raw values and normalized values/qualifiers appear together in the report.

## Candidates, conflicts and observation dates

Additive `fact_candidates` and `fact_candidate_claims` tables store deterministic
candidate identities and append-only evidence links. IDs hash venue identity,
field, qualifiers, normalized value and normalizer version. Every candidate
retains constant internal rights and `status=fact_candidate`. Existing IDs
remain stable for previously supported field shapes.

`facts` exposes currently qualified records with `status=fact`. The candidate
assessment persists `qualification_basis` and `current_supporting_claim_ids`.
Historical evidence links remain append-only.

| Qualification basis | Required evidence |
| --- | --- |
| `official` | An eligible first-party claim, exact reviewed approval, matching venue, and the persisted requested and final URL authorization tuple with a final URL on the approved domain. |
| `independent_agreement` | Two eligible, independently reviewed sources that agree on the entity, field, exact qualifiers, and normalized value. Review-only web sources may qualify through this basis. |

| Review state | Meaning |
| --- | --- |
| `awaiting_review` | Qualifying evidence with no comparable disagreement. |
| `needs_confirmation` | Potential support exists and comparable claims disagree; show `확인 필요`. |
| `evidence_hold` | A previously qualifying candidate has lost its supporting evidence or approval. It is retained, including all historical links. Any remaining conflict is still shown as `확인 필요`. |

All normalizable retained claims participate in disagreements, including NULL
provenance and revisions of one page. Eligibility is displayed per claim.
Two votes for A plus a claim for B keep the A candidate blocked for
confirmation; two independent pairs supporting A and B keep both candidates
in the same conflict group. No claim or candidate is deleted, no current value
is selected, and no conflict is automatically resolved. Invalid values remain
diagnostic evidence rather than being assigned an invented comparison group.

The collector stores `seen_at`, not post/page publication dates. This version
always returns `date_basis: seen_at`. Conflict `newest_claim_ids` and per-claim
`is_newest` mean **most recently observed**, using parsed timezone-aware instants.
All tied observations are marked. Missing, invalid or timezone-naive timestamps
are unranked; execution time is never substituted. Evidence older than 90 days
shows `오래됨`. Freshness and recency do not grant authority or resolve conflicts.

## Commands and persistence

Use disposable external paths for inspection and fixtures:

```bash
HANSET_IG_DIR=/tmp/hanset-review-fixture \
  python3 -m hanset_active collect promote \
  --policy /tmp/hanset-review-policy.json --dry-run

HANSET_IG_DIR=/tmp/hanset-review-fixture \
  python3 -m hanset_active collect promote \
  --policy /tmp/hanset-review-policy.json

HANSET_IG_DIR=/tmp/hanset-review-fixture \
  python3 -m hanset_active collect report \
  --policy /tmp/hanset-review-policy.json
```

`report` and `promote --dry-run` use SQLite read-only connections, do not create
missing state directories/databases, do not migrate old schemas, and reassess
current policy without persisting. Without `--policy`, report still lists
claims/conflicts, grants no eligibility, and shows existing candidates on hold.
A supplied invalid policy returns only a safe error. The output preserves
`claims` and `conflicts`, and adds `facts`, `fact_candidates`, normalized evidence,
independence rationales, diagnostic counts and policy/assessment fingerprints.
The assessment fingerprint excludes time-dependent freshness labels. Reports
can include newly qualifying proposed candidates before any write. In
read-only assessment, created/changed timestamps describe the previously
persisted row. Promotion returns the saved timestamps. Offline review also
ignores unrelated model-selection configuration.

Default manual and scheduled web runs reassess all current claims at completion,
including partial and already-complete runs. This recovers claims committed by
an earlier interrupted run. The assessment checks stored official bindings
against current default target authorization. Ad hoc `--targets` runs skip
automatic assessment and can use explicit offline review.

Automatic assessment and daily metrics read `HANSET_COLLECT_PROMOTION_POLICY`
when set, otherwise they use `HANSET_IG_DIR/promotion_policy.json`. Web status
reports `promotion_policy_absent`, `promotion_policy_invalid`, or
`promotion_authorization_unavailable` when assessment is deferred. A valid
policy with an empty approvals list moves existing candidates to evidence hold.

Both write paths hold the existing collector `daemon.lock` and assess and
persist candidates and links in one SQLite write transaction. A failed write
reports `promotion_write_failed` in web status and rolls back candidate changes.
Committed claims and the publication handoff proceed independently. An identical
rerun leaves IDs, counts and change timestamps unchanged. New corroboration
appends links. Revocation retains candidates on hold. Migration alone generates
no candidate and does not rewrite claim evidence or infer legacy official
authority. The trigger still rejects new NULL provenance inserts while existing
NULL rows remain untouched.

Daemon validation uses synthetic temporary databases only:

```bash
python3 -m pytest tests/active_bot tests/twin -q
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tools/chrome-clipper/tests/*.test.mjs
python3 site/build_site.py --release
```

Record release page counts and `leak check OK` in validation results. No browser
code changes are involved. Never run these commands against the operations DB.
