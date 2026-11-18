-- Online-Termine: öffentliche Seite der Praxis (/p/termin/<kurzname>), Anfrage mit SMS-Code, Bestätigung durch den Empfang.

CREATE TABLE booking_settings (
    practice_id INTEGER PRIMARY KEY REFERENCES practices(id),
    enabled INTEGER NOT NULL DEFAULT 0,
    days_ahead INTEGER NOT NULL DEFAULT 21 CHECK (days_ahead BETWEEN 1 AND 90),
    lead_hours INTEGER NOT NULL DEFAULT 12 CHECK (lead_hours BETWEEN 0 AND 168),
    slot_step INTEGER NOT NULL DEFAULT 15 CHECK (slot_step IN (5, 10, 15, 20, 30, 60)),
    show_therapists INTEGER NOT NULL DEFAULT 0,
    intro_de TEXT,
    intro_fr TEXT,
    updated_at TEXT
);

-- Welche Behandlungsarten und welche Therapeut/innen online buchbar sind
CREATE TABLE booking_types (
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    treatment_type_id INTEGER NOT NULL,
    PRIMARY KEY (practice_id, treatment_type_id)
);
CREATE TABLE booking_therapists (
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (practice_id, user_id)
);

CREATE TABLE booking_requests (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (status IN ('unverified', 'requested', 'confirmed', 'declined', 'expired')),
    treatment_type_id INTEGER NOT NULL,
    therapist_id INTEGER NOT NULL REFERENCES users(id),
    location_id INTEGER,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    birthdate TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT,
    email_ok INTEGER NOT NULL DEFAULT 0,
    lang TEXT NOT NULL DEFAULT 'de',
    insurance TEXT,
    has_prescription INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    code_hash TEXT,
    code_expires_at TEXT,
    code_attempts INTEGER NOT NULL DEFAULT 0,
    verified_at TEXT,
    patient_id INTEGER REFERENCES patients(id),
    appointment_id INTEGER,
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT,
    decline_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX booking_requests_open ON booking_requests(practice_id, status, starts_at);
