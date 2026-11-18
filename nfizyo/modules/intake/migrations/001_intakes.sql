-- Anliegen: Die Patientin / der Patient beschreibt das Problem in der eigenen Sprache
-- (Körperstelle, Schmerz, seit wann, wann schlimmer, Ziele, eigener Text).
-- Strukturierte Antworten sind sprachunabhängige Codes – sie erscheinen sofort auf Deutsch/Französisch.
CREATE TABLE intakes (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    appointment_id INTEGER REFERENCES appointments(id),
    channel TEXT NOT NULL DEFAULT 'app' CHECK (channel IN ('app', 'kiosk', 'staff')),
    lang TEXT NOT NULL,
    regions TEXT,
    pain INTEGER CHECK (pain BETWEEN 0 AND 10),
    since TEXT CHECK (since IN ('days', 'weeks', 'months', 'years')),
    worse TEXT,
    goals TEXT,
    text_original TEXT,
    text_translated TEXT,
    translated_to TEXT,
    translation_engine TEXT,
    translation_warning TEXT,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'seen', 'taken')),
    seen_by INTEGER REFERENCES users(id),
    seen_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX intakes_patient ON intakes(practice_id, patient_id, status);
