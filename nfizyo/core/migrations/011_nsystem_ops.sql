-- NSystem-Betrieb (0.9.0): Supportzugang nur mit Einverständnis der Leitung, Support-Anfragen, Wartungshinweise und ein
-- Protokoll aller NSystem-Vorgänge auf diesem Praxis-Server. Sichtbar für die Leitung (Einstellungen › NSystem).
-- Keine Patientendaten: Anfragen werden vor dem Speichern geprüft (core/patientdata.py), blockierter Text wird nie gespeichert.

-- Supportzugang (WireGuard-Tunnel zum NSystem-Hub + SSH nur über den Tunnel). Geöffnet/geschlossen vom Runner (root).
CREATE TABLE box_support_sessions (
    id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,            -- Auftrag an den Runner (runner/local/<id>.json)
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    opened_by INTEGER NOT NULL REFERENCES users(id),
    hours INTEGER NOT NULL CHECK (hours BETWEEN 1 AND 8),
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested', 'open', 'closed', 'failed')),
    opened_at TEXT,
    until TEXT,                                 -- Ablauf (UTC, ISO)
    closed_at TEXT,
    closed_by INTEGER REFERENCES users(id),     -- leer = abgelaufen / NSystem / Runner
    close_reason TEXT,                          -- early | expired | nsystem | failed
    error TEXT
);
CREATE INDEX box_support_sessions_status ON box_support_sessions(status, requested_at);

-- Protokoll: jeder NSystem-Vorgang (Supportzugang geöffnet/geschlossen/Verbindung/Befehl über SSH, Anfrage gesendet,
-- Antwort erhalten, Wartungshinweis erhalten …). Nur anhängbar; «reported» = an die NSystem-Übersicht gemeldet.
CREATE TABLE box_events (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    practice_id INTEGER REFERENCES practices(id),   -- leer = ganzer Praxis-Server
    user_id INTEGER REFERENCES users(id),
    detail TEXT,                                    -- JSON, kurz, ohne Patientendaten
    reported INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX box_events_at ON box_events(at);
CREATE TRIGGER box_events_no_delete BEFORE DELETE ON box_events
BEGIN
    SELECT RAISE(ABORT, 'box_events ist nur anhängbar');
END;
CREATE TRIGGER box_events_no_change BEFORE UPDATE OF uid, at, kind, practice_id, user_id, detail ON box_events
BEGIN
    SELECT RAISE(ABORT, 'box_events ist nur anhängbar');
END;

-- Support-Anfragen der Praxis an NSystem (Hilfe › Support anfragen) und die Antwort von NSystem.
CREATE TABLE box_support_requests (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    area TEXT NOT NULL CHECK (area IN ('agenda', 'documentation', 'billing', 'patientapp', 'technical', 'other')),
    urgency TEXT NOT NULL CHECK (urgency IN ('low', 'normal', 'urgent')),
    body TEXT NOT NULL,
    with_access INTEGER NOT NULL DEFAULT 0,      -- Leitung hat gleich den Supportzugang geöffnet
    created_at TEXT NOT NULL,
    sent_at TEXT,                                -- an NSystem übermittelt
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'in_progress', 'done')),
    reply TEXT,
    reply_by TEXT,
    reply_at TEXT,
    reply_seen_at TEXT,
    rev INTEGER NOT NULL DEFAULT 0               -- Stand der Antwort von NSystem
);
CREATE INDEX box_support_requests_practice ON box_support_requests(practice_id, created_at);

-- Wartungshinweise von NSystem (Banner für das Personal, nicht für Patient/innen), ausblendbar pro Person.
CREATE TABLE box_notices (
    id TEXT PRIMARY KEY,
    text_de TEXT NOT NULL,
    text_fr TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    withdrawn_at TEXT
);

CREATE TABLE box_notice_dismissals (
    notice_id TEXT NOT NULL REFERENCES box_notices(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    dismissed_at TEXT NOT NULL,
    PRIMARY KEY (notice_id, user_id)
);
