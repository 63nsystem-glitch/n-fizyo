-- Abwesenheiten der Therapeut/innen (Ferien, Krankheit, Weiterbildung, Andere) und Schliessungen der Praxis
-- (Betriebsferien, einzelne Schliesstage). Ganze Tage oder ein Zeitfenster an jedem Tag des Zeitraums.
-- Keine medizinischen Angaben: die Bemerkung ist kurz und freiwillig.
CREATE TABLE absences (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    user_id INTEGER REFERENCES users(id),                         -- NULL = ganze Praxis (Schliessung)
    kind TEXT NOT NULL CHECK (kind IN ('vacation', 'sick', 'training', 'other', 'closure')),
    starts_on TEXT NOT NULL,                                       -- YYYY-MM-DD (Ortszeit)
    ends_on TEXT NOT NULL,
    start_time TEXT,                                               -- HH:MM; beide leer = ganzer Tag
    end_time TEXT,
    note TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (ends_on >= starts_on),
    CHECK ((user_id IS NULL) = (kind = 'closure')),
    CHECK ((start_time IS NULL) = (end_time IS NULL))
);
CREATE INDEX absences_range ON absences(practice_id, starts_on, ends_on);

-- Feiertage: Kanton der Praxis (leer = Kanton aus Einstellungen › Praxis) und einzeln aus-/eingeschaltete Tage.
CREATE TABLE holiday_settings (
    practice_id INTEGER PRIMARY KEY REFERENCES practices(id),
    canton TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE holiday_overrides (
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    canton TEXT NOT NULL,
    code TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    PRIMARY KEY (practice_id, canton, code)
);
