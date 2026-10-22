-- HANSET members (Google sign-up). Applied by the operations bot to the D1 database `hanset-members`.
-- google_sub = Google account ID (the `sub` claim). Repeat logins keep the original newsletter_opt_in.
CREATE TABLE IF NOT EXISTS members (
  google_sub TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  name TEXT,
  newsletter_opt_in INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
