-- Verlaufsdokumentation: Notizen mit unveränderlichen Versionen.
-- Nach der Freigabe ist eine Notiz gesperrt; Änderungen nur als Nachtrag (neue Version mit Grund).

CREATE TABLE notes (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    appointment_id INTEGER REFERENCES appointments(id),
    author_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL DEFAULT 'session' CHECK (kind IN ('session', 'initial', 'discharge', 'interpreter')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved')),
    source TEXT NOT NULL DEFAULT 'typed' CHECK (source IN ('typed', 'dictation', 'interpreter')),
    recording_id INTEGER,
    session_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT,
    approved_by INTEGER REFERENCES users(id)
);
CREATE INDEX notes_patient ON notes(practice_id, patient_id, session_date);
CREATE INDEX notes_author ON notes(practice_id, author_id, status);

CREATE TABLE note_versions (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    note_id INTEGER NOT NULL REFERENCES notes(id),
    version INTEGER NOT NULL,
    befund TEXT NOT NULL DEFAULT '',
    behandlung TEXT NOT NULL DEFAULT '',
    beurteilung TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT '',
    reason TEXT,
    edited_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (note_id, version)
);

CREATE TRIGGER note_versions_no_update BEFORE UPDATE ON note_versions
BEGIN
    SELECT RAISE(ABORT, 'Notiz-Versionen sind unveränderlich');
END;

CREATE TRIGGER note_versions_no_delete BEFORE DELETE ON note_versions
WHEN (SELECT status FROM notes WHERE id = OLD.note_id) = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'Notiz-Versionen sind unveränderlich');
END;

CREATE TRIGGER notes_stay_approved BEFORE UPDATE OF status ON notes
WHEN OLD.status = 'approved' AND NEW.status != 'approved'
BEGIN
    SELECT RAISE(ABORT, 'Freigegebene Notizen bleiben freigegeben');
END;

CREATE TRIGGER notes_no_delete BEFORE DELETE ON notes
WHEN OLD.status = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'Freigegebene Notizen können nicht gelöscht werden');
END;

CREATE TABLE note_templates (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    name TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'de',
    befund TEXT NOT NULL DEFAULT '',
    behandlung TEXT NOT NULL DEFAULT '',
    beurteilung TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT ''
);
