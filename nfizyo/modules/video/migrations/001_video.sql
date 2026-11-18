-- Video-Sitzungen über den eigenen Jitsi-Server der Praxis (Schweiz). Es wird nichts aufgezeichnet.
-- Der Raumname ist zufällig; den Link für die Patientin / den Patienten gibt es nur als Hash (steht allein in der SMS).
CREATE TABLE video_rooms (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    room TEXT NOT NULL UNIQUE,
    link_hash TEXT UNIQUE,
    sms_sent_at TEXT,
    therapist_joined_at TEXT,
    patient_joined_at TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (practice_id, appointment_id)
);

CREATE TABLE video_consents (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    via TEXT NOT NULL CHECK (via IN ('app', 'link')),
    consented_at TEXT NOT NULL,
    UNIQUE (practice_id, patient_id)
);
