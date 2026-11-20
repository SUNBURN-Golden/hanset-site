ALTER TABLE seed_venues RENAME TO seed_venues_previous;
CREATE TABLE seed_venues (
    seed_venue_id TEXT PRIMARY KEY, seed_id TEXT NOT NULL, name TEXT,
    normalized_name TEXT, domain TEXT, website TEXT, frozen_at TEXT,
    UNIQUE(seed_id, normalized_name, domain, website)
);
INSERT INTO seed_venues SELECT * FROM seed_venues_previous ORDER BY rowid;
DROP TABLE seed_venues_previous;
