-- Nachrichten zwischen Praxis und Patient/in (pro Patient ein Verlauf), inkl. "Problem melden".
-- direction: in = vom Patienten, out = von der Praxis. Übersetzung wird zusätzlich gespeichert, Original bleibt.
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    kind TEXT NOT NULL DEFAULT 'message' CHECK (kind IN ('message', 'problem')),
    author_user_id INTEGER REFERENCES users(id),
    body TEXT NOT NULL,
    lang TEXT NOT NULL,
    translation TEXT,
    translation_lang TEXT,
    translation_engine TEXT,
    translation_warning TEXT,
    region TEXT,
    pain INTEGER,
    read_at TEXT,
    handled_at TEXT,
    handled_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE INDEX messages_patient ON messages(practice_id, patient_id, id);
CREATE INDEX messages_unread ON messages(practice_id, direction, read_at);
