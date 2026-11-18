-- Benachrichtigungen an Patient/innen (neue Nachricht, neue Information, neuer Termin) – nur Hinweis + App-Link, kein Inhalt.

-- Wahl der Praxis: aus | nur E-Mail (mit Einverständnis) | nur SMS | E-Mail mit Einverständnis, sonst SMS (Standard)
CREATE TABLE notify_settings (
    practice_id INTEGER PRIMARY KEY REFERENCES practices(id),
    mode TEXT NOT NULL DEFAULT 'email_or_sms' CHECK (mode IN ('off', 'email', 'sms', 'email_or_sms')),
    appointments INTEGER NOT NULL DEFAULT 0,          -- auch bei neuen Terminen (ausser die Erinnerung kommt ohnehin)
    updated_by INTEGER REFERENCES users(id),
    updated_at TEXT
);

-- Warteschlange: ein Eintrag pro Anlass. Mehrere Anlässe einer Person werden zu EINER Benachrichtigung gebündelt.
CREATE TABLE notify_queue (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    kind TEXT NOT NULL CHECK (kind IN ('message', 'info', 'appointment')),
    ref_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'skipped')),
    reason TEXT,                                      -- bei skipped: off | no_app | no_consent | no_phone | no_contact | read | reminder
    channel TEXT CHECK (channel IN ('sms', 'email')), -- geplant (pending) bzw. benutzt (sent)
    due_at TEXT NOT NULL,                             -- frühestens dann senden (UTC)
    created_at TEXT NOT NULL,
    sent_at TEXT,
    outbox_id INTEGER
);
CREATE INDEX notify_queue_due ON notify_queue(status, due_at);
CREATE INDEX notify_queue_patient ON notify_queue(practice_id, patient_id, id);
CREATE INDEX notify_queue_ref ON notify_queue(practice_id, kind, ref_id);
