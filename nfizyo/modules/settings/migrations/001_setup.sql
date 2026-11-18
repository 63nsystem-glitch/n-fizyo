-- Einrichtungs-Checkliste der Leitung (Übersicht): ausgeblendet, sobald alles erledigt ist und die Leitung sie ausblendet.
CREATE TABLE practice_setup (
    practice_id INTEGER PRIMARY KEY REFERENCES practices(id),
    dismissed_at TEXT,
    dismissed_by INTEGER REFERENCES users(id)
);
