-- Diktate: Audio (vorübergehend) → Transkript → Notizentwurf.
-- Audio wird nach der Freigabe gelöscht (Einstellung NFIZYO_DELETE_AUDIO_AFTER_APPROVAL).
CREATE TABLE recordings (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER REFERENCES patients(id),
    appointment_id INTEGER REFERENCES appointments(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    client_uuid TEXT NOT NULL,
    path TEXT,
    mime TEXT,
    size INTEGER,
    language TEXT NOT NULL DEFAULT 'de',
    status TEXT NOT NULL DEFAULT 'uploaded'
        CHECK (status IN ('uploaded', 'transcribing', 'transcribed', 'drafting', 'drafted', 'failed', 'approved')),
    transcript TEXT,
    transcript_engine TEXT,
    draft_json TEXT,
    note_id INTEGER REFERENCES notes(id),
    error TEXT,
    recorded_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    audio_deleted_at TEXT,
    UNIQUE (practice_id, client_uuid)
);
CREATE INDEX recordings_user ON recordings(practice_id, user_id, status);
