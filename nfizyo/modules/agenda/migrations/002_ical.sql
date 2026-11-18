-- Kalender-Abo (iCal) pro Therapeut/in: geheimer Link, jederzeit widerrufbar. Gespeichert wird nur der Hash des Links.
CREATE TABLE ical_tokens (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT,
    detail TEXT NOT NULL DEFAULT 'initials' CHECK (detail IN ('initials', 'none')),
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX ical_tokens_user ON ical_tokens(practice_id, user_id);
