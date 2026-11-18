-- Information für Patient/innen: aus dem Diktat (oder selbst geschrieben), von der Therapie freigegeben, übersetzt.

CREATE TABLE patient_infos (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    recording_id INTEGER,                                -- Diktat, aus dem der Vorschlag stammt (falls vorhanden)
    author_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'generating'
        CHECK (status IN ('generating', 'draft', 'approved', 'discarded')),
    source_lang TEXT NOT NULL,                           -- Sprache der Therapeutin / des Therapeuten
    home TEXT,                                           -- «Was Sie zu Hause tun können»
    contact TEXT,                                        -- «Wann Sie sich melden sollten»
    ai_engine TEXT,
    ai_warnings TEXT,
    target_lang TEXT,                                    -- Sprache der Patientin / des Patienten (App)
    home_translated TEXT,
    contact_translated TEXT,
    translation_engine TEXT,
    translation_warning TEXT,
    approved_by INTEGER REFERENCES users(id),
    approved_at TEXT,
    read_at TEXT,                                        -- von der Patientin / vom Patienten geöffnet
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX patient_infos_patient ON patient_infos(practice_id, patient_id, id);
CREATE UNIQUE INDEX patient_infos_recording ON patient_infos(practice_id, recording_id) WHERE recording_id IS NOT NULL;
