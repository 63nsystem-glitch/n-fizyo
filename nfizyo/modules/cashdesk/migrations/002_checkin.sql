-- Abo-Besuche: auch durch Check-in der Patientin / des Patienten in der App (MTT), dann ohne Mitarbeitende.
-- SQLite kann NOT NULL nicht entfernen → Tabelle neu anlegen und Daten übernehmen.
ALTER TABLE subscription_visits RENAME TO subscription_visits_v1;
CREATE TABLE subscription_visits (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    visited_on TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),                     -- leer bei Check-in in der App
    source TEXT NOT NULL DEFAULT 'desk' CHECK (source IN ('desk', 'checkin')),
    created_at TEXT NOT NULL,
    CHECK (created_by IS NOT NULL OR source = 'checkin'),
    UNIQUE (subscription_id, visited_on)
);
INSERT INTO subscription_visits (id, practice_id, subscription_id, visited_on, created_by, source, created_at)
    SELECT id, practice_id, subscription_id, visited_on, created_by, 'desk', created_at FROM subscription_visits_v1;
DROP TABLE subscription_visits_v1;
