-- Messreihen, die die Therapeutin / der Therapeut für die Patienten-App freigibt (nur Zahlen und Ziel, keine Diagnose).
-- joint/side: '' statt NULL, damit UNIQUE greift.
CREATE TABLE measurement_shares (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    kind TEXT NOT NULL,
    joint TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL DEFAULT '',
    shared_by INTEGER REFERENCES users(id),
    shared_at TEXT NOT NULL,
    UNIQUE (practice_id, patient_id, kind, joint, side)
);
