-- HANSET venue digital twin. Facts are append-only; a change closes the old period.
CREATE TABLE IF NOT EXISTS entities(
 entity_id TEXT PRIMARY KEY,
 entity_type TEXT NOT NULL CHECK(entity_type IN ('venue','branch','facility','price_plan','hours','usage_condition','program')),
 parent_id TEXT REFERENCES entities(entity_id),
 kind TEXT,                      -- facility kind, hours scope, condition kind, ticket key
 name TEXT,
 qualifiers TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(qualifiers)),
 created_at TEXT NOT NULL,
 origin TEXT NOT NULL            -- verified_csv | active_store
);
CREATE TABLE IF NOT EXISTS aliases(entity_id TEXT NOT NULL REFERENCES entities(entity_id), alias TEXT NOT NULL, normalized TEXT NOT NULL, lang TEXT, PRIMARY KEY(entity_id,normalized));
CREATE TABLE IF NOT EXISTS external_ids(entity_id TEXT NOT NULL REFERENCES entities(entity_id), system TEXT NOT NULL, external_id TEXT NOT NULL, PRIMARY KEY(entity_id,system,external_id));
CREATE TABLE IF NOT EXISTS facts(
 fact_id TEXT PRIMARY KEY,
 entity_id TEXT NOT NULL REFERENCES entities(entity_id),
 attribute TEXT NOT NULL,
 slot_key TEXT NOT NULL,         -- entity|attribute|source scope; one open value per slot per source
 value_json TEXT CHECK(value_json IS NULL OR json_valid(value_json)),
 value_hash TEXT NOT NULL,
 unit TEXT,
 status TEXT NOT NULL CHECK(status IN ('fact','source_claim','ai_inference','unverified')),
 source_url TEXT,
 source_type TEXT NOT NULL CHECK(source_type IN ('own_site','official_directory','third_party','aggregator','sns','attachment','verified_csv','none')),
 evidence_locator TEXT NOT NULL CHECK(json_valid(evidence_locator) AND evidence_locator!='{}'),
 quote TEXT CHECK(quote IS NULL OR length(quote)<=240),
 origin_ref TEXT NOT NULL,       -- obs_id or csv row ref
 collected_at TEXT NOT NULL,
 effective_from TEXT NOT NULL,
 effective_to TEXT,
 superseded_by TEXT REFERENCES facts(fact_id) DEFERRABLE INITIALLY DEFERRED,
 closed_reason TEXT,
 rights TEXT NOT NULL CHECK(rights IN ('public_page_factual','internal_review_only','none')),
 retention_until TEXT,           -- quote/evidence text retention; the fact row itself keeps history
 last_confirmed_at TEXT,
 created_at TEXT NOT NULL,
 CHECK(status!='unverified' OR value_json IS NULL),
 CHECK(value_json IS NOT NULL OR status='unverified'),
 CHECK(status NOT IN ('fact','source_claim') OR source_url IS NOT NULL),
 CHECK(status!='fact' OR source_type='own_site'),
 CHECK(effective_to IS NULL OR effective_to>=effective_from)
);
CREATE INDEX IF NOT EXISTS facts_open ON facts(entity_id,attribute) WHERE effective_to IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS facts_slot_value ON facts(slot_key,value_hash,effective_from);
CREATE TRIGGER IF NOT EXISTS facts_close_only BEFORE UPDATE ON facts
WHEN OLD.effective_to IS NOT NULL AND (NEW.effective_to IS NOT OLD.effective_to OR NEW.superseded_by IS NOT OLD.superseded_by)
 OR NEW.value_json IS NOT OLD.value_json OR NEW.status IS NOT OLD.status OR NEW.source_url IS NOT OLD.source_url
 OR NEW.effective_from IS NOT OLD.effective_from OR NEW.entity_id IS NOT OLD.entity_id OR NEW.attribute IS NOT OLD.attribute
BEGIN SELECT RAISE(ABORT,'facts are append-only; close a period once'); END;
CREATE TRIGGER IF NOT EXISTS facts_no_delete BEFORE DELETE ON facts BEGIN SELECT RAISE(ABORT,'facts are never deleted'); END;
CREATE TABLE IF NOT EXISTS corrections(correction_id TEXT PRIMARY KEY, entity_id TEXT, attribute TEXT, old_fact_id TEXT REFERENCES facts(fact_id), new_fact_id TEXT REFERENCES facts(fact_id), kind TEXT CHECK(kind IN ('value_change','evidence_found','evidence_removed')), detected_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conflicts(conflict_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, attribute TEXT NOT NULL, fact_ids TEXT NOT NULL CHECK(json_valid(fact_ids)), reason TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved','obsolete')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_queue(review_id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL, details TEXT NOT NULL CHECK(json_valid(details)), state TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_rights(domain TEXT PRIMARY KEY, source_type TEXT, fields TEXT NOT NULL CHECK(json_valid(fields)), permission TEXT NOT NULL, refresh_days INTEGER, retention TEXT NOT NULL, supported INTEGER NOT NULL, unsupported_reason TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stage_checkpoints(stage TEXT PRIMARY KEY, cursor TEXT, run_id TEXT, state TEXT, stats TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS stage_failures(failure_id TEXT PRIMARY KEY, stage TEXT NOT NULL, item_ref TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL CHECK(state IN ('retry','dead','done')), updated_at TEXT NOT NULL, UNIQUE(stage,item_ref));
CREATE TABLE IF NOT EXISTS cost_ledger(id INTEGER PRIMARY KEY, run_id TEXT, stage TEXT, amount_krw INTEGER NOT NULL CHECK(amount_krw=0), note TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS daemon_runs(run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, state TEXT, stats TEXT);
CREATE TABLE IF NOT EXISTS service_views(branch_id TEXT PRIMARY KEY REFERENCES entities(entity_id), view_json TEXT NOT NULL CHECK(json_valid(view_json)), computed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quality_issues(issue_id TEXT PRIMARY KEY, check_id TEXT NOT NULL, ref TEXT NOT NULL, details TEXT, state TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
-- Daemon work tables (pipeline state, resumable per stage).
CREATE TABLE IF NOT EXISTS work_items(item_id TEXT PRIMARY KEY, origin TEXT NOT NULL CHECK(origin IN ('active_store','verified_csv')), canonical_url TEXT NOT NULL, source_url TEXT, fetched_at TEXT NOT NULL, content_hash TEXT NOT NULL, changed INTEGER, seq INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidates(cand_id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES work_items(item_id), seq INTEGER NOT NULL, subject_ref TEXT NOT NULL, branch_id TEXT REFERENCES entities(entity_id), field TEXT NOT NULL, value_json TEXT, unit TEXT, label TEXT, source_type TEXT, source_url TEXT, quote TEXT, locator TEXT, origin_ref TEXT NOT NULL, observed_at TEXT NOT NULL, verdict TEXT, verdict_reason TEXT);
CREATE TABLE IF NOT EXISTS candidate_branches(subject_ref TEXT PRIMARY KEY, name TEXT, name_ko TEXT, website TEXT, city TEXT, origin TEXT, branch_id TEXT REFERENCES entities(entity_id), resolution TEXT, updated_at TEXT);
