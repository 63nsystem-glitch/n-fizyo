-- MediData-Webservice nach «Leistungsabrechnung Schnittstellendokumentation» V5.2.0 (02.09.2026):
-- Teilnehmerverzeichnis (3.6), Dokumentenstatus (3.8), Dokumente abholen und bestätigen (3.9), Benachrichtigungen (3.10).
-- Alles je Praxis (practice_id): der MediData-Zugang der Box gehört einer Praxis (NFIZYO_MEDIDATA_PRACTICE).

-- ---------------------------------------------------------------- Sendungen: Referenzen und Stand bei MediData
-- document_reference = eigene, eindeutige Dokumentbezeichnung (Steuerdatei «documentReference», = guid der Sendung)
-- correlation_reference = Geschäftsfall (Steuerdatei «correlationReference», = Rechnungsnummer)
-- md_status = letzter Stand laut GET …/uploads/{transmissionReference}/status: PROCESSING | DONE | ERROR | NOT_FOUND
-- resolution = wie ein «unklarer» Versand geklärt wurde (duplicate | notification | response | status)
ALTER TABLE invoice_transmissions ADD COLUMN document_reference TEXT;
ALTER TABLE invoice_transmissions ADD COLUMN correlation_reference TEXT;
ALTER TABLE invoice_transmissions ADD COLUMN error_code TEXT;
ALTER TABLE invoice_transmissions ADD COLUMN md_status TEXT;
ALTER TABLE invoice_transmissions ADD COLUMN md_error_reason TEXT;
ALTER TABLE invoice_transmissions ADD COLUMN md_detail TEXT;
ALTER TABLE invoice_transmissions ADD COLUMN md_modified TEXT;
ALTER TABLE invoice_transmissions ADD COLUMN md_checked_at TEXT;
ALTER TABLE invoice_transmissions ADD COLUMN resolution TEXT;
CREATE INDEX invoice_transmissions_reference ON invoice_transmissions(practice_id, reference);
CREATE INDEX invoice_transmissions_docref ON invoice_transmissions(practice_id, document_reference);

-- ---------------------------------------------------------------- Teilnehmerverzeichnis (GET /ela/participants, alle 24 h)
CREATE TABLE medidata_participants (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    gln_participant TEXT NOT NULL,
    gln_receiver TEXT NOT NULL,          -- muss als invoice:transport@to verwendet werden
    name TEXT NOT NULL,
    street TEXT,
    zip TEXT,
    town TEXT,
    law_types TEXT,                      -- JSON [1..6]
    bag_number INTEGER,
    tg_tp_change INTEGER NOT NULL DEFAULT 0,
    additional_costs INTEGER NOT NULL DEFAULT 0,
    max_receive INTEGER,
    not_supported TEXT,                  -- JSON: nicht unterstützte Dokument-MIME-Types
    tg_allowed INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    UNIQUE (practice_id, gln_participant, gln_receiver)
);
CREATE INDEX medidata_participants_receiver ON medidata_participants(practice_id, gln_receiver);

-- ---------------------------------------------------------------- Dokumente abholen (GET /ela/downloads, alle 30 min)
-- state: imported (in invoice_responses übernommen) | stored (nicht unterstützter Typ, aufbewahrt und angezeigt) | error
-- confirmed_at: PUT …/downloads/{ref}/status {"status": "CONFIRMED"} erst NACH dem Speichern (gleiche Transaktion wie der Import).
CREATE TABLE medidata_downloads (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    transmission_reference TEXT NOT NULL,
    document_reference TEXT,
    correlation_reference TEXT,
    sender_gln TEXT,
    doc_type TEXT,
    file_size INTEGER,
    modus TEXT,
    created TEXT,
    state TEXT NOT NULL CHECK (state IN ('imported', 'stored', 'error')),
    response_id INTEGER REFERENCES invoice_responses(id),
    invoice_id INTEGER REFERENCES invoices(id),
    error TEXT,
    content BLOB,
    sha256 TEXT,
    fetched_at TEXT NOT NULL,
    confirmed_at TEXT,
    seen_by INTEGER REFERENCES users(id),
    seen_at TEXT,
    UNIQUE (practice_id, transmission_reference)
);

-- ---------------------------------------------------------------- Benachrichtigungen (GET /ela/notifications, alle 30 min)
CREATE TABLE medidata_notifications (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    notification_id INTEGER NOT NULL,     -- «id» von MediData
    severity TEXT,                        -- INFO | WARNING | ERROR
    subject TEXT,                         -- JSON {de, fr, it}
    message TEXT,                         -- JSON {de, fr, it}
    potential_reasons TEXT,               -- JSON
    possible_solutions TEXT,              -- JSON
    technical_information TEXT,
    template TEXT,
    mode TEXT,                            -- TEST | PRODUCTION
    error_code TEXT,
    transmission_reference TEXT,
    document_reference TEXT,
    correlation_reference TEXT,
    created TEXT,
    invoice_id INTEGER REFERENCES invoices(id),
    transmission_id INTEGER REFERENCES invoice_transmissions(id),
    fetched_at TEXT NOT NULL,
    confirmed_at TEXT,                    -- PUT …/notifications/{id}/status {"notificationFetched": true}
    seen_by INTEGER REFERENCES users(id),
    seen_at TEXT,
    UNIQUE (practice_id, notification_id)
);
CREATE INDEX medidata_notifications_open ON medidata_notifications(practice_id, seen_at);

-- ---------------------------------------------------------------- Abfrage-Stand je Praxis und Dienst
-- kind: participants | status | downloads | notifications
CREATE TABLE medidata_sync (
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    kind TEXT NOT NULL CHECK (kind IN ('participants', 'status', 'downloads', 'notifications')),
    last_run_at TEXT,
    last_ok_at TEXT,
    last_error TEXT,
    count INTEGER,
    PRIMARY KEY (practice_id, kind)
);
