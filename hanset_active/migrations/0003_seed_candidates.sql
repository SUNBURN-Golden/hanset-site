CREATE TABLE seed_candidates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    address TEXT NOT NULL,
    sido TEXT NOT NULL,
    sigungu TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_url TEXT NOT NULL,
    official_domain TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('new','resolved','duplicate','rejected','promoted')),
    reject_reason TEXT,
    normalized_name TEXT NOT NULL,
    normalized_address TEXT NOT NULL,
    single_venue INTEGER NOT NULL DEFAULT 0,
    matched_kind TEXT CHECK (matched_kind IN ('venue','frozen')),
    matched_id TEXT
);
CREATE INDEX seed_candidates_name ON seed_candidates(normalized_name);
CREATE INDEX seed_candidates_address ON seed_candidates(normalized_address);
CREATE INDEX seed_candidates_domain ON seed_candidates(official_domain);
CREATE INDEX seed_candidates_status ON seed_candidates(status);

CREATE TABLE seed_candidate_sources (
    record_key TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES seed_candidates(id),
    source_kind TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetch_url TEXT,
    official INTEGER NOT NULL CHECK (official IN (0,1)),
    ownership_evidence TEXT,
    label_status TEXT NOT NULL CHECK (label_status IN ('source_claim','unverified')),
    verification TEXT NOT NULL CHECK (verification = '미확인'),
    fields_json TEXT NOT NULL,
    revision TEXT NOT NULL
);
CREATE INDEX seed_candidate_sources_candidate ON seed_candidate_sources(candidate_id);

ALTER TABLE request_log ADD COLUMN adapter TEXT;
