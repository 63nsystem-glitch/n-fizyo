-- NSystem-Übersicht 0.9.0: geplante Befehle (Update zu einer Uhrzeit), Supportzugang (Tunnel-Adresse pro Box),
-- Protokoll der Boxen, Support-Anfragen (Eingang), Wartungshinweise, eigener Zustand (z. B. Sicherung des Panels).
-- Weiterhin keine Patientendaten: Anfragen mit erkennbaren Patientendaten werden ohne Text gespeichert.

-- deliver_after: leer = sofort, sonst erst ab dann an die Box · notice_id: automatischer Wartungshinweis (geplantes Update)
ALTER TABLE commands ADD COLUMN deliver_after REAL;
ALTER TABLE commands ADD COLUMN notice_id TEXT;
-- Supportzugang: öffentlicher WireGuard-Schlüssel und Tunnel-Adresse der Box (z. B. 10.77.0.12)
ALTER TABLE boxes ADD COLUMN wg_pubkey TEXT;
ALTER TABLE boxes ADD COLUMN wg_ip TEXT;

CREATE TABLE box_events (
    id INTEGER PRIMARY KEY,
    box_id TEXT NOT NULL REFERENCES boxes(id),
    uid TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT,
    received_at TEXT NOT NULL,
    UNIQUE (box_id, uid)
);
CREATE INDEX box_events_box ON box_events(box_id, at);

CREATE TABLE support_requests (
    id INTEGER PRIMARY KEY,
    box_id TEXT NOT NULL REFERENCES boxes(id),
    uid TEXT NOT NULL,
    practice TEXT,                               -- Kurzname der Praxis auf der Box
    from_name TEXT,                              -- Name der Person in der Praxis (Personal)
    from_role TEXT,
    area TEXT NOT NULL,
    urgency TEXT NOT NULL,
    body TEXT,                                   -- leer, wenn blockiert
    blocked TEXT,                                -- erkannte Kategorien (ohne den Text selbst)
    with_access INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'in_progress', 'done')),
    reply TEXT,
    reply_by TEXT,
    reply_at TEXT,
    rev INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE (box_id, uid)
);
CREATE INDEX support_requests_status ON support_requests(status, received_at);

CREATE TABLE notices (
    id TEXT PRIMARY KEY,
    text_de TEXT NOT NULL,
    text_fr TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    all_boxes INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL,
    withdrawn_at TEXT
);

CREATE TABLE notice_boxes (
    notice_id TEXT NOT NULL REFERENCES notices(id),
    box_id TEXT NOT NULL REFERENCES boxes(id),
    PRIMARY KEY (notice_id, box_id)
);

CREATE TABLE central_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
