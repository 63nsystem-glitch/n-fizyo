-- Abrechnung, Stufe 2: QR-Rechnung, XML 5.0 / MediData, Zahlungseingang (camt.054), Mahnungen.

-- Zahlungsangaben der Praxis (IBAN oder QR-IBAN), Zahlungsfrist, Mahnwesen, XML-Modus
CREATE TABLE billing_settings (
    practice_id INTEGER PRIMARY KEY REFERENCES practices(id),
    iban TEXT,
    payment_days INTEGER NOT NULL DEFAULT 30 CHECK (payment_days BETWEEN 5 AND 120),
    reminder_days INTEGER NOT NULL DEFAULT 10 CHECK (reminder_days BETWEEN 5 AND 60),
    reminder_fee_2 REAL NOT NULL DEFAULT 0 CHECK (reminder_fee_2 >= 0),
    reminder_fee_3 REAL NOT NULL DEFAULT 0 CHECK (reminder_fee_3 >= 0),
    xml_modus TEXT NOT NULL DEFAULT 'test' CHECK (xml_modus IN ('test', 'production')),
    copy_to_insurer INTEGER NOT NULL DEFAULT 1,   -- Tiers garant: Rechnungskopie elektronisch an den Versicherer
    updated_at TEXT
);

-- Beim Abschliessen festgehalten (danach unveränderbar wie die Rechnung selbst)
ALTER TABLE invoices ADD COLUMN qr_reference TEXT;
ALTER TABLE invoices ADD COLUMN iban TEXT;
ALTER TABLE invoices ADD COLUMN due_on TEXT;
ALTER TABLE invoice_lines ADD COLUMN external_factor REAL NOT NULL DEFAULT 1.0;

CREATE TRIGGER invoices_payment_locked BEFORE UPDATE ON invoices
WHEN OLD.status != 'draft' AND (NEW.qr_reference IS NOT OLD.qr_reference OR NEW.iban IS NOT OLD.iban)
BEGIN
    SELECT RAISE(ABORT, 'Zahlungsangaben einer abgeschlossenen Rechnung sind unveränderbar');
END;

-- Zahlungseingänge: aus der Bankdatei (camt.054/053) oder von Hand. Ohne Zuordnung (invoice_id NULL) = zu prüfen.
CREATE TABLE invoice_payments (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    invoice_id INTEGER REFERENCES invoices(id),
    amount_chf REAL NOT NULL,
    booked_on TEXT NOT NULL,
    reference TEXT,
    debtor TEXT,
    source TEXT NOT NULL CHECK (source IN ('camt', 'manual')),
    bank_ref TEXT,                       -- eindeutige Kennung der Buchung (gegen doppeltes Einlesen)
    import_id INTEGER,
    note TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (practice_id, bank_ref)
);
CREATE INDEX invoice_payments_invoice ON invoice_payments(practice_id, invoice_id);

CREATE TABLE payment_imports (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    entries INTEGER NOT NULL DEFAULT 0,
    matched INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (practice_id, sha256)
);

-- Mahnungen (Stufe 1 = Zahlungserinnerung, 2 und 3 mit Gebühr laut Einstellungen)
CREATE TABLE invoice_reminders (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    level INTEGER NOT NULL CHECK (level BETWEEN 1 AND 3),
    fee_chf REAL NOT NULL DEFAULT 0,
    open_chf REAL NOT NULL,
    due_on TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (practice_id, invoice_id, level)
);

-- Elektronische Übermittlung (XML 5.0): erzeugt, über MediData gesendet, Fehler
CREATE TABLE invoice_transmissions (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    kind TEXT NOT NULL CHECK (kind IN ('invoice', 'copy', 'reminder', 'storno')),
    reminder_level INTEGER,
    guid TEXT NOT NULL,
    modus TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('file', 'medidata')),
    status TEXT NOT NULL CHECK (status IN ('created', 'sent', 'failed')),
    reference TEXT,
    error TEXT,
    xml TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX invoice_transmissions_invoice ON invoice_transmissions(practice_id, invoice_id);
