PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS entities(entity_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL,
 parent_id TEXT REFERENCES entities(entity_id), attributes TEXT NOT NULL CHECK(json_valid(attributes)));
CREATE TABLE IF NOT EXISTS aliases(entity_id TEXT REFERENCES entities(entity_id), language TEXT NOT NULL,
 alias TEXT NOT NULL, PRIMARY KEY(entity_id,language,alias));
CREATE TABLE IF NOT EXISTS facts(
 fact_id TEXT PRIMARY KEY NOT NULL,
 subject_id TEXT NOT NULL REFERENCES entities(entity_id),
 subject_type TEXT NOT NULL,
 attribute TEXT NOT NULL,
 qualifiers TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(qualifiers)),
 value TEXT CHECK(value IS NULL OR json_valid(value)),
 value_text TEXT NOT NULL,
 unit TEXT,
 value_state TEXT NOT NULL CHECK(value_state IN ('known','unknown','not_applicable')),
 source_url TEXT,
 source_type TEXT NOT NULL CHECK(source_type IN ('official_page','official_social','venue_reply','operator_profile','third_party','ai_extraction','legacy_site_content')),
 evidence_locator TEXT NOT NULL CHECK(json_valid(evidence_locator) AND evidence_locator != '{}'),
 collected_at TEXT NOT NULL,
 valid_from TEXT,
 valid_to TEXT,
 status TEXT NOT NULL CHECK(status IN ('verified_fact','source_claim','ai_inference','unverified')),
 conflict_group TEXT,
 supersedes_fact_id TEXT REFERENCES facts(fact_id),
 correction_of TEXT REFERENCES facts(fact_id),
 collection_permission TEXT NOT NULL CHECK(collection_permission IN ('public_page_factual','venue_reply_internal','manual_review_only')),
 retention_policy TEXT NOT NULL CHECK(retention_policy IN ('facts_keep_history','review_365_days','limited_raw')),
 retention_until TEXT,
 retention_days INTEGER CHECK(retention_days IS NULL OR retention_days>0),
 extractor_id TEXT NOT NULL,
 run_id TEXT NOT NULL,
 created_at TEXT NOT NULL,
 closed_reason TEXT,
 closed_by_fact_id TEXT REFERENCES facts(fact_id) DEFERRABLE INITIALLY DEFERRED,
 CHECK(source_type!='ai_extraction' OR status IN ('ai_inference','unverified')),
 CHECK(value_state='known' OR (value IS NULL AND status='unverified')),
 CHECK(status!='unverified' OR value IS NULL),
 CHECK(status!='verified_fact' OR (source_url IS NOT NULL AND source_type='official_page')),
 CHECK(source_url IS NOT NULL OR (status!='verified_fact' AND source_type IN ('venue_reply','legacy_site_content','third_party','ai_extraction','official_social','operator_profile'))),
 CHECK(value IS NULL OR lower(trim(value,'" ')) NOT IN ('','no limit','unlimited')),
 CHECK(value_state!='known' OR lower(trim(value_text)) NOT IN ('','not confirmed','no limit','unlimited')),
 CHECK(valid_from IS NULL OR valid_to IS NULL OR julianday(valid_to)>=julianday(valid_from)),
 CHECK(julianday(collected_at) IS NOT NULL AND substr(collected_at,-6)='+09:00'),
 CHECK(valid_from IS NULL OR julianday(valid_from) IS NOT NULL),
 CHECK(valid_to IS NULL OR julianday(valid_to) IS NOT NULL)
);
CREATE TRIGGER IF NOT EXISTS facts_immutable BEFORE UPDATE ON facts
WHEN OLD.valid_to IS NOT NULL OR NEW.valid_to IS NULL OR (NEW.fact_id IS NOT OLD.fact_id OR NEW.subject_id IS NOT OLD.subject_id OR NEW.subject_type IS NOT OLD.subject_type OR NEW.attribute IS NOT OLD.attribute OR NEW.qualifiers IS NOT OLD.qualifiers OR NEW.value IS NOT OLD.value OR NEW.value_text IS NOT OLD.value_text OR NEW.unit IS NOT OLD.unit OR NEW.value_state IS NOT OLD.value_state OR NEW.source_url IS NOT OLD.source_url OR NEW.source_type IS NOT OLD.source_type OR NEW.evidence_locator IS NOT OLD.evidence_locator OR NEW.collected_at IS NOT OLD.collected_at OR NEW.valid_from IS NOT OLD.valid_from OR NEW.status IS NOT OLD.status OR NEW.conflict_group IS NOT OLD.conflict_group OR NEW.supersedes_fact_id IS NOT OLD.supersedes_fact_id OR NEW.correction_of IS NOT OLD.correction_of OR NEW.collection_permission IS NOT OLD.collection_permission OR NEW.retention_policy IS NOT OLD.retention_policy OR NEW.retention_until IS NOT OLD.retention_until OR NEW.retention_days IS NOT OLD.retention_days OR NEW.extractor_id IS NOT OLD.extractor_id OR NEW.run_id IS NOT OLD.run_id OR NEW.created_at IS NOT OLD.created_at)
BEGIN SELECT RAISE(ABORT,'facts are append-only; close once'); END;
CREATE TRIGGER IF NOT EXISTS facts_no_delete BEFORE DELETE ON facts
BEGIN SELECT RAISE(ABORT,'facts are append-only'); END;
CREATE TABLE IF NOT EXISTS fact_confirmations(confirmation_id TEXT PRIMARY KEY,
 fact_id TEXT NOT NULL REFERENCES facts(fact_id), collected_at TEXT NOT NULL,
 evidence_locator TEXT NOT NULL CHECK(json_valid(evidence_locator)), run_id TEXT NOT NULL, source_url TEXT, source_type TEXT NOT NULL, status TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS confirmations_no_update BEFORE UPDATE ON fact_confirmations
BEGIN SELECT RAISE(ABORT,'confirmations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS confirmations_no_delete BEFORE DELETE ON fact_confirmations
BEGIN SELECT RAISE(ABORT,'confirmations are append-only'); END;
CREATE TABLE IF NOT EXISTS review_items(review_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('conflict','verification','identity')),
 conflict_group TEXT, fact_ids TEXT NOT NULL CHECK(json_valid(fact_ids)), state TEXT NOT NULL CHECK(state IN ('open','resolved','dismissed')),
 resolution_notes TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE VIEW IF NOT EXISTS current_facts AS SELECT f.* FROM facts f WHERE valid_to IS NULL
 AND NOT EXISTS(SELECT 1 FROM facts n WHERE n.supersedes_fact_id=f.fact_id OR n.correction_of=f.fact_id);
CREATE VIEW IF NOT EXISTS state_changes AS SELECT fact_id,subject_id,attribute,qualifiers,value,value_state,
 valid_from,valid_to,supersedes_fact_id,correction_of,closed_reason,collected_at FROM facts;
CREATE TABLE IF NOT EXISTS run_locks(stage TEXT PRIMARY KEY, owner TEXT NOT NULL, heartbeat TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints(stage TEXT, source_id TEXT, unit_id TEXT, cursor TEXT,
 completed_at TEXT NOT NULL, PRIMARY KEY(stage,source_id,unit_id));
CREATE TABLE IF NOT EXISTS failure_queue(failure_id TEXT PRIMARY KEY,stage TEXT NOT NULL,source_id TEXT NOT NULL,
 unit_id TEXT NOT NULL,reason TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
 next_retry_at TEXT,state TEXT NOT NULL CHECK(state IN ('pending','dead','resolved')));
CREATE TABLE IF NOT EXISTS budgets(scope TEXT,period TEXT,requests INTEGER NOT NULL DEFAULT 0,
 bytes INTEGER NOT NULL DEFAULT 0,ai_tokens INTEGER NOT NULL DEFAULT 0,ai_cost_units INTEGER NOT NULL DEFAULT 0,
 max_requests INTEGER NOT NULL,max_bytes INTEGER NOT NULL,max_ai_tokens INTEGER NOT NULL,max_ai_cost_units INTEGER NOT NULL,
 PRIMARY KEY(scope,period),CHECK(requests>=0 AND bytes>=0 AND ai_tokens>=0 AND ai_cost_units>=0),
 CHECK(max_requests>=0 AND max_bytes>=0 AND max_ai_tokens>=0 AND max_ai_cost_units>=0),CHECK(requests<=max_requests AND bytes<=max_bytes AND ai_tokens<=max_ai_tokens AND ai_cost_units<=max_ai_cost_units));
