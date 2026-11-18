-- Firmenkunden: Rechnungen an Firmen (z. B. Rückenschule im Betrieb, Ergonomie-Beratung, Firmen-Abos).
-- Eigene Nummernfolge (F2026-0001), Preise wie bei Selbstzahlern als Endpreise inkl. MWST (nur mit MWST-Nummer der Praxis).
CREATE TABLE firms (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    name TEXT NOT NULL,
    department TEXT,
    contact TEXT,
    street TEXT,
    house_no TEXT,
    zip TEXT,
    city TEXT,
    email TEXT,
    phone TEXT,
    uid TEXT,
    their_reference TEXT,
    payment_days INTEGER CHECK (payment_days IS NULL OR payment_days BETWEEN 0 AND 120),
    lang TEXT NOT NULL DEFAULT 'de' CHECK (lang IN ('de', 'fr')),
    note TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX firms_practice ON firms(practice_id, active, name);

CREATE TABLE firm_invoices (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    firm_id INTEGER NOT NULL REFERENCES firms(id),
    number TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'final', 'cancelled')),
    subject TEXT,
    their_reference TEXT,
    period_from TEXT,
    period_to TEXT,
    lang TEXT NOT NULL DEFAULT 'de' CHECK (lang IN ('de', 'fr')),
    total_chf REAL NOT NULL DEFAULT 0,
    vat_chf REAL NOT NULL DEFAULT 0,
    vat_number TEXT,
    iban TEXT,
    qr_reference TEXT,
    due_on TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    finalized_at TEXT,
    finalized_by INTEGER REFERENCES users(id),
    cancelled_at TEXT,
    cancel_reason TEXT,
    UNIQUE (practice_id, number)
);
CREATE INDEX firm_invoices_firm ON firm_invoices(practice_id, firm_id);
CREATE INDEX firm_invoices_status ON firm_invoices(practice_id, status);

CREATE TABLE firm_invoice_lines (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    invoice_id INTEGER NOT NULL REFERENCES firm_invoices(id),
    service_date TEXT,
    price_item_id INTEGER,
    code TEXT,
    label TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'service' CHECK (kind IN ('service', 'product')),
    quantity REAL NOT NULL DEFAULT 1,
    unit_price REAL NOT NULL,
    vat_rate REAL NOT NULL DEFAULT 0,
    amount_chf REAL NOT NULL
);
CREATE INDEX firm_invoice_lines_invoice ON firm_invoice_lines(practice_id, invoice_id);

CREATE TABLE firm_payments (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    invoice_id INTEGER NOT NULL REFERENCES firm_invoices(id),
    amount_chf REAL NOT NULL,
    booked_on TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('manual', 'camt')),
    method TEXT NOT NULL DEFAULT 'bank' CHECK (method IN ('bank', 'cash', 'twint', 'card')),
    reference TEXT,
    debtor TEXT,
    bank_ref TEXT,
    note TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE INDEX firm_payments_invoice ON firm_payments(practice_id, invoice_id);
CREATE UNIQUE INDEX firm_payments_bank_ref ON firm_payments(practice_id, bank_ref) WHERE bank_ref IS NOT NULL;

CREATE TABLE firm_reminders (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    invoice_id INTEGER NOT NULL REFERENCES firm_invoices(id),
    level INTEGER NOT NULL CHECK (level BETWEEN 1 AND 3),
    fee_chf REAL NOT NULL DEFAULT 0,
    open_chf REAL NOT NULL,
    due_on TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (invoice_id, level)
);

-- Abgeschlossene Rechnungen: unveränderbar (nur Storno), Zeilen gesperrt, nicht löschbar
CREATE TRIGGER firm_invoices_locked BEFORE UPDATE ON firm_invoices
WHEN OLD.status != 'draft' AND (NEW.status = 'draft' OR OLD.status = 'cancelled' OR NEW.total_chf IS NOT OLD.total_chf
    OR NEW.vat_chf IS NOT OLD.vat_chf OR NEW.number IS NOT OLD.number OR NEW.firm_id IS NOT OLD.firm_id
    OR NEW.qr_reference IS NOT OLD.qr_reference OR NEW.iban IS NOT OLD.iban OR NEW.due_on IS NOT OLD.due_on
    OR NEW.subject IS NOT OLD.subject OR NEW.their_reference IS NOT OLD.their_reference)
BEGIN
    SELECT RAISE(ABORT, 'Firmenrechnung ist abgeschlossen');
END;
CREATE TRIGGER firm_invoices_no_delete BEFORE DELETE ON firm_invoices WHEN OLD.status != 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Abgeschlossene Firmenrechnungen können nicht gelöscht werden');
END;
CREATE TRIGGER firm_lines_locked_ins BEFORE INSERT ON firm_invoice_lines
WHEN (SELECT status FROM firm_invoices WHERE id = NEW.invoice_id) != 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Rechnungszeilen sind abgeschlossen');
END;
CREATE TRIGGER firm_lines_locked_upd BEFORE UPDATE ON firm_invoice_lines
WHEN (SELECT status FROM firm_invoices WHERE id = OLD.invoice_id) != 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Rechnungszeilen sind abgeschlossen');
END;
CREATE TRIGGER firm_lines_locked_del BEFORE DELETE ON firm_invoice_lines
WHEN (SELECT status FROM firm_invoices WHERE id = OLD.invoice_id) != 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Rechnungszeilen sind abgeschlossen');
END;
-- Zahlungen und Mahnungen nur auf abgeschlossene Rechnungen; einmal erfasst, bleiben sie (Korrektur per Gegenbuchung)
CREATE TRIGGER firm_payments_final BEFORE INSERT ON firm_payments
WHEN (SELECT status FROM firm_invoices WHERE id = NEW.invoice_id) != 'final'
BEGIN
    SELECT RAISE(ABORT, 'Zahlung nur auf abgeschlossene Rechnungen');
END;
CREATE TRIGGER firm_payments_no_change BEFORE UPDATE ON firm_payments
BEGIN
    SELECT RAISE(ABORT, 'Zahlungen sind unveränderbar');
END;
CREATE TRIGGER firm_payments_no_delete BEFORE DELETE ON firm_payments
BEGIN
    SELECT RAISE(ABORT, 'Zahlungen sind unveränderbar');
END;
CREATE TRIGGER firm_reminders_final BEFORE INSERT ON firm_reminders
WHEN (SELECT status FROM firm_invoices WHERE id = NEW.invoice_id) != 'final'
BEGIN
    SELECT RAISE(ABORT, 'Mahnung nur auf abgeschlossene Rechnungen');
END;
