-- MTT (Medizinische Trainingstherapie): Geräte mit QR-Code, Trainingsplan pro Patient/in, Besuche und Sätze aus der App,
-- Vorschläge zur Steigerung (die Therapeutin / der Therapeut entscheidet).
CREATE TABLE mtt_machines (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    location_id INTEGER,                                  -- Standort (Agenda), optional
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'strength' CHECK (kind IN ('strength', 'cardio', 'free')),
    step REAL NOT NULL DEFAULT 2.5,                       -- kleinste Gewichtsstufe (kg)
    qr_token TEXT NOT NULL UNIQUE,                        -- steht im QR-Code am Gerät
    position INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX mtt_machines_practice ON mtt_machines(practice_id, active);

CREATE TABLE mtt_plans (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    author_id INTEGER REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'ended')),
    goal TEXT,
    approved_at TEXT,
    approved_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX mtt_plans_patient ON mtt_plans(practice_id, patient_id, status);

CREATE TABLE mtt_items (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    plan_id INTEGER NOT NULL REFERENCES mtt_plans(id),
    machine_id INTEGER NOT NULL REFERENCES mtt_machines(id),
    sets INTEGER,
    reps INTEGER,
    load REAL,                                            -- kg
    minutes INTEGER,                                      -- Ausdauer
    setting TEXT,                                         -- Einstellung am Gerät, z. B. «Sitz 4, Lehne 2»
    note TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    removed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX mtt_items_plan ON mtt_items(practice_id, plan_id);

CREATE TABLE mtt_visits (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    plan_id INTEGER REFERENCES mtt_plans(id),
    day TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    source TEXT NOT NULL DEFAULT 'app' CHECK (source IN ('app', 'qr', 'desk')),
    UNIQUE (practice_id, patient_id, day)
);

CREATE TABLE mtt_sets (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    visit_id INTEGER NOT NULL REFERENCES mtt_visits(id),
    item_id INTEGER NOT NULL REFERENCES mtt_items(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    reps INTEGER,
    load REAL,
    minutes INTEGER,
    effort INTEGER CHECK (effort BETWEEN 0 AND 10),       -- «Wie anstrengend?» 0–10
    pain INTEGER CHECK (pain BETWEEN 0 AND 10),
    created_at TEXT NOT NULL
);
CREATE INDEX mtt_sets_item ON mtt_sets(practice_id, item_id, created_at);
CREATE INDEX mtt_sets_visit ON mtt_sets(practice_id, visit_id);

CREATE TABLE mtt_suggestions (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    item_id INTEGER NOT NULL REFERENCES mtt_items(id),
    kind TEXT NOT NULL CHECK (kind IN ('increase', 'decrease', 'hold')),
    reason TEXT NOT NULL,
    from_load REAL,
    to_load REAL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'dismissed')),
    created_at TEXT NOT NULL,
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT
);
CREATE INDEX mtt_suggestions_open ON mtt_suggestions(practice_id, status, patient_id);
