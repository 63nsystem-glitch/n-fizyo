-- Klinische Akte: Befunde am Körper und Messwerte (Stammdaten, Versicherer, Zuweiser und Verordnungen liegen im Kern)

-- Probleme am Körper: Diagnosen, Operationen, Schmerzen, Einschränkungen, Freigaben, Komplikationen
CREATE TABLE findings (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    kind TEXT NOT NULL CHECK (kind IN ('diagnosis', 'surgery', 'finding', 'pain', 'restriction', 'clearance',
                                       'complication', 'red_flag', 'goal')),
    title TEXT NOT NULL,
    detail TEXT,
    structure_id TEXT,
    region TEXT,
    side TEXT CHECK (side IN ('l', 'r', 'b')),
    spine_levels TEXT,                         -- z. B. "l4,l5"
    severity INTEGER,
    on_date TEXT,                              -- Datum (z. B. Operation, Freigabe ab)
    until TEXT,                                -- gültig bis (Einschränkungen)
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'resolved')),
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'document', 'intake', 'dictation', 'app')),
    source_document_id INTEGER,
    source_excerpt TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX findings_patient ON findings(practice_id, patient_id, status);

CREATE TABLE measurements (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    note_id INTEGER,
    kind TEXT NOT NULL,
    joint TEXT,
    side TEXT,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    goal REAL,
    measured_on TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE INDEX measurements_patient ON measurements(practice_id, patient_id, kind, measured_on);
