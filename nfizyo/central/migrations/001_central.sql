-- NSystem-Übersicht: Personal, Boxen, Meldungen, Befehle, Warnungen, Protokoll. Keine Patientendaten.
CREATE TABLE staff (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    totp_secret TEXT,
    totp_pending TEXT,
    totp_enabled_at TEXT,
    totp_last_step INTEGER,
    recovery_codes TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    stage TEXT NOT NULL DEFAULT 'ok',
    csrf TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE boxes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key_enc TEXT NOT NULL,              -- Gerätegeheimnis, verschlüsselt (core/crypto.py)
    note TEXT,
    created_at TEXT NOT NULL,
    last_report_at TEXT,
    last_version TEXT,
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE reports (
    id INTEGER PRIMARY KEY,
    box_id TEXT NOT NULL REFERENCES boxes(id),
    received_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX reports_box ON reports(box_id, id);

CREATE TABLE commands (
    id TEXT PRIMARY KEY,
    box_id TEXT NOT NULL REFERENCES boxes(id),
    action TEXT NOT NULL,
    args TEXT NOT NULL DEFAULT '{}',
    issued_by TEXT NOT NULL,
    staff_id INTEGER REFERENCES staff(id),
    issued_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    signature TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'running', 'done', 'failed', 'rejected', 'expired')),
    result TEXT,
    delivered_at TEXT,
    finished_at TEXT
);
CREATE INDEX commands_box ON commands(box_id, issued_at);

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY,
    box_id TEXT NOT NULL REFERENCES boxes(id),
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    pushed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX alerts_open ON alerts(box_id, kind, closed_at);

CREATE TABLE audit (
    id INTEGER PRIMARY KEY,
    staff_id INTEGER,
    action TEXT NOT NULL,
    box_id TEXT,
    detail TEXT,
    at TEXT NOT NULL
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'nur anhängbar'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'nur anhängbar'); END;
