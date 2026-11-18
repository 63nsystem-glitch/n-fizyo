-- Kern: Stammdaten der Patientinnen und Patienten, Versicherer, Zuweisende, Verordnungen,
-- Behandlungsteam und Vertretungen (wer darf welche Akte sehen).

CREATE TABLE insurers (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    name TEXT NOT NULL,
    law TEXT CHECK (law IN ('KVG', 'UVG', 'IVG', 'MVG', 'VVG')),
    gln TEXT,
    tiers TEXT NOT NULL DEFAULT 'garant' CHECK (tiers IN ('garant', 'payant')),
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX insurers_practice ON insurers(practice_id);

CREATE TABLE referrers (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    name TEXT NOT NULL,
    practice_name TEXT,
    specialty TEXT,
    address TEXT,
    phone TEXT,
    email TEXT,
    hin_email TEXT,
    gln TEXT,
    zsr TEXT,
    lang TEXT NOT NULL DEFAULT 'de',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX referrers_practice ON referrers(practice_id);

CREATE TABLE patients (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    number TEXT NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    birthdate TEXT,
    sex TEXT CHECK (sex IN ('f', 'm', 'x')),
    lang TEXT NOT NULL DEFAULT 'de',          -- Sprache von App und Korrespondenz
    spoken_lang TEXT,                          -- gesprochene Sprache (für Übersetzung/Dolmetscher)
    needs_interpreter INTEGER NOT NULL DEFAULT 0,
    phone TEXT,
    email TEXT,
    address TEXT,
    zip TEXT,
    city TEXT,
    insurer_id INTEGER REFERENCES insurers(id),
    law TEXT CHECK (law IN ('KVG', 'UVG', 'IVG', 'MVG', 'VVG', 'privat')),
    insured_number TEXT,                       -- Versichertennummer / Kartennummer
    case_number TEXT,                          -- Schadennummer (UVG)
    accident_date TEXT,
    employer TEXT,
    referrer_id INTEGER REFERENCES referrers(id),
    therapist_id INTEGER REFERENCES users(id),
    goal TEXT,                                 -- Therapieziel in Worten des Patienten
    admin_note TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (practice_id, number)
);
CREATE INDEX patients_name ON patients(practice_id, last_name, first_name);

CREATE TABLE prescriptions (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    referrer_id INTEGER REFERENCES referrers(id),
    issued_on TEXT,
    sessions INTEGER NOT NULL DEFAULT 9,
    kind TEXT NOT NULL DEFAULT 'physio' CHECK (kind IN ('physio', 'complex', 'domicile', 'lymph', 'group')),
    diagnosis TEXT,
    document_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'used', 'expired')),
    created_at TEXT NOT NULL
);
CREATE INDEX prescriptions_patient ON prescriptions(practice_id, patient_id);

-- Behandlungsteam: Therapeut/innen, die eine Patientin / einen Patienten behandeln.
-- Nur wer im Team ist (oder eine gültige Vertretung hat), sieht die medizinische Akte.
CREATE TABLE patient_team (
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    since TEXT NOT NULL,
    added_by INTEGER REFERENCES users(id),
    reason TEXT NOT NULL DEFAULT 'manual' CHECK (reason IN ('manual', 'main', 'appointment')),
    PRIMARY KEY (practice_id, patient_id, user_id)
);
CREATE INDEX patient_team_user ON patient_team(practice_id, user_id);

-- Vertretung (Ferien, Krankheit): from_user gibt to_user für eine Zeit Zugriff auf alle seine Patient/innen.
CREATE TABLE delegations (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    from_user INTEGER NOT NULL REFERENCES users(id),
    to_user INTEGER NOT NULL REFERENCES users(id),
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL,
    note TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    CHECK (from_user != to_user),
    CHECK (ends_on >= starts_on)
);
CREATE INDEX delegations_to ON delegations(practice_id, to_user);

-- Installierte Modulversionen (für Updates und Rückkehr zur Vorversion)
CREATE TABLE module_versions (
    module TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
