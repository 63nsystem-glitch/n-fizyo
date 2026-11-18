-- Agenda: Standorte, Räume, Geräte, Behandlungsarten, Arbeitszeiten, Termine, Warteliste

CREATE TABLE locations (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    name TEXT NOT NULL,
    address TEXT,
    phone TEXT,
    color TEXT NOT NULL DEFAULT '#2F5DA8',
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE rooms (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE devices (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE treatment_types (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    name_de TEXT NOT NULL,
    name_fr TEXT NOT NULL,
    minutes INTEGER NOT NULL DEFAULT 30,
    tariff_code TEXT,
    needs_device INTEGER NOT NULL DEFAULT 0,
    color TEXT NOT NULL DEFAULT '#2F5DA8',
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE therapist_hours (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    location_id INTEGER REFERENCES locations(id),
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL
);
CREATE INDEX therapist_hours_user ON therapist_hours(practice_id, user_id, weekday);

CREATE TABLE appointment_series (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    prescription_id INTEGER REFERENCES prescriptions(id),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE appointments (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER REFERENCES patients(id),         -- leer = Blocker (Pause, Sitzung, Ferien)
    therapist_id INTEGER NOT NULL REFERENCES users(id),
    location_id INTEGER REFERENCES locations(id),
    room_id INTEGER REFERENCES rooms(id),
    device_id INTEGER REFERENCES devices(id),
    treatment_type_id INTEGER REFERENCES treatment_types(id),
    prescription_id INTEGER REFERENCES prescriptions(id),
    series_id INTEGER REFERENCES appointment_series(id),
    starts_at TEXT NOT NULL,                             -- UTC
    ends_at TEXT NOT NULL,                               -- UTC
    status TEXT NOT NULL DEFAULT 'booked'
        CHECK (status IN ('booked', 'arrived', 'done', 'cancelled', 'no_show', 'blocked')),
    title TEXT,                                          -- für Blocker
    note TEXT,
    cancel_reason TEXT,
    reminder_sent_at TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX appointments_time ON appointments(practice_id, starts_at);
CREATE INDEX appointments_patient ON appointments(practice_id, patient_id, starts_at);
CREATE INDEX appointments_therapist ON appointments(practice_id, therapist_id, starts_at);

CREATE TABLE waitlist (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER REFERENCES patients(id),
    name TEXT,
    phone TEXT,
    wish TEXT,
    preferred TEXT,                                      -- z. B. "Mo/Mi morgens"
    therapist_id INTEGER REFERENCES users(id),
    location_id INTEGER REFERENCES locations(id),
    treatment_type_id INTEGER REFERENCES treatment_types(id),
    source TEXT NOT NULL DEFAULT 'reception' CHECK (source IN ('reception', 'app', 'phone')),
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
