-- 0.9.0: auch Verordnungen und offene Posten übernehmen (CHECK der Art erweitert – SQLite: Tabelle neu anlegen, Daten bleiben).
ALTER TABLE import_jobs RENAME TO import_jobs_old;
CREATE TABLE import_jobs (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    kind TEXT NOT NULL CHECK (kind IN ('patients', 'insurers', 'referrers', 'appointments', 'prescriptions', 'balances')),
    filename TEXT NOT NULL,
    path TEXT,
    rows INTEGER NOT NULL DEFAULT 0,
    mapping TEXT,
    preview TEXT,
    result TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'done', 'discarded')),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    done_at TEXT
);
INSERT INTO import_jobs (id, practice_id, kind, filename, path, rows, mapping, preview, result, status, created_by, created_at, done_at)
    SELECT id, practice_id, kind, filename, path, rows, mapping, preview, result, status, created_by, created_at, done_at FROM import_jobs_old;
DROP TABLE import_jobs_old;
CREATE INDEX import_jobs_practice ON import_jobs(practice_id, id);
