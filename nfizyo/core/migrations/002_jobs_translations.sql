-- Hintergrundaufgaben (Transkription, Notizentwurf, Berichtsauswertung, OCR, Übersetzung).
-- Ein Arbeiter nach dem anderen: die KI-Hardware der Box wird nicht überlastet.
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX jobs_queue ON jobs(status, id);

-- Zwischenspeicher für maschinelle Übersetzungen (Übungen, Nachrichten, Anliegen).
-- Die Quelle bleibt immer sichtbar; die Übersetzung ist als "maschinell" markiert.
CREATE TABLE translations (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    source_hash TEXT NOT NULL,
    source_lang TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    text TEXT NOT NULL,
    engine TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (practice_id, source_hash, source_lang, target_lang)
);
