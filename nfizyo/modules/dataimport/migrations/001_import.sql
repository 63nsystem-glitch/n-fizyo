-- Datenübernahme: eine Zeile pro hochgeladener Datei (Datei selbst liegt verschlüsselt auf der Box-Platte bis zur Übernahme)
CREATE TABLE import_jobs (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    kind TEXT NOT NULL CHECK (kind IN ('patients', 'insurers', 'referrers', 'appointments')),
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
CREATE INDEX import_jobs_practice ON import_jobs(practice_id, id);
