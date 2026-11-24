ALTER TABLE request_log ADD COLUMN transfer_complete INTEGER NOT NULL DEFAULT 0;

CREATE INDEX request_log_run ON request_log(run_id);

CREATE TABLE active_evidence_ready (
    raw_id TEXT PRIMARY KEY REFERENCES raw_documents(raw_id),
    recorded_at TEXT NOT NULL
);
CREATE INDEX active_evidence_ready_time ON active_evidence_ready(recorded_at);
