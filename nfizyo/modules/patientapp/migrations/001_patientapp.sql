-- Patienten-App: Einladung mit Link + 6-stelligem Code, Einwilligung, Anmeldecodes
CREATE TABLE patient_invites (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    nonce TEXT NOT NULL UNIQUE,
    code_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE patient_consents (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    kind TEXT NOT NULL CHECK (kind IN ('app', 'translation')),
    version TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX patient_consents_patient ON patient_consents(practice_id, patient_id);

CREATE TABLE patient_login_codes (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    code_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);
