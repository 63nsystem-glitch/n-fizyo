-- Heimprogramm: eigenes Video der Patientin / des Patienten (nur mit Einwilligung), Übungen «bei wenig Zeit zuerst»,
-- Gründe, warum nicht geübt wurde (die Patientin / der Patient wählt selbst).
ALTER TABLE program_items ADD COLUMN essential INTEGER NOT NULL DEFAULT 0;
ALTER TABLE program_items ADD COLUMN video_path TEXT;
ALTER TABLE program_items ADD COLUMN video_mime TEXT;
ALTER TABLE program_items ADD COLUMN video_size INTEGER;
ALTER TABLE program_items ADD COLUMN video_consent_at TEXT;
ALTER TABLE program_items ADD COLUMN video_by INTEGER REFERENCES users(id);

CREATE TABLE exercise_skips (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    day TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('forgot', 'unclear', 'no_time', 'pain', 'app', 'no_benefit')),
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (practice_id, patient_id, day)
);
CREATE INDEX exercise_skips_patient ON exercise_skips(practice_id, patient_id, day);
