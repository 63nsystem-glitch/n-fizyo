-- Heimprogramm: Übungsbibliothek (DE/FR), Programme pro Patient, Erledigt-Protokoll aus der App
CREATE TABLE exercises (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    code TEXT,
    name_de TEXT NOT NULL,
    name_fr TEXT NOT NULL,
    instructions_de TEXT NOT NULL,
    instructions_fr TEXT NOT NULL,
    regions TEXT,                 -- Körperbereiche (z. B. knee,hip)
    icon TEXT NOT NULL DEFAULT 'fitness_center',
    sets INTEGER,
    reps INTEGER,
    hold_sec INTEGER,
    per_day INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE programs (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    author_id INTEGER REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'ended')),
    approved_at TEXT,
    approved_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX programs_patient ON programs(practice_id, patient_id, status);

CREATE TABLE program_items (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    program_id INTEGER NOT NULL REFERENCES programs(id),
    exercise_id INTEGER NOT NULL REFERENCES exercises(id),
    sets INTEGER,
    reps INTEGER,
    hold_sec INTEGER,
    per_day INTEGER NOT NULL DEFAULT 1,
    note TEXT,                    -- Hinweis der Therapeutin (in Praxissprache, wird für die App übersetzt)
    note_lang TEXT NOT NULL DEFAULT 'de',
    position INTEGER NOT NULL DEFAULT 0,
    removed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE exercise_logs (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    item_id INTEGER NOT NULL REFERENCES program_items(id),
    done_on TEXT NOT NULL,
    pain INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX exercise_logs_patient ON exercise_logs(practice_id, patient_id, done_on);
