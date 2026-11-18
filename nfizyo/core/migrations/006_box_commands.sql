-- NSystem-Anbindung: Befehle von NSystem (signiert) und ihr Ergebnis. Für die Leitung jeder Praxis auf dieser Box sichtbar.
CREATE TABLE box_commands (
    id TEXT PRIMARY KEY,                -- vom NSystem vergeben (UUID)
    action TEXT NOT NULL,
    args TEXT NOT NULL DEFAULT '{}',
    issued_by TEXT NOT NULL,            -- Name der Person bei NSystem
    issued_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'rejected')),
    reason TEXT,                        -- bei «rejected»: warum abgelehnt
    result TEXT,                        -- Kurzfassung (ohne Patientendaten)
    finished_at TEXT,
    reported INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX box_commands_received ON box_commands(received_at);

-- Zustand der Anbindung (letzte Meldung, Fehler)
CREATE TABLE box_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
