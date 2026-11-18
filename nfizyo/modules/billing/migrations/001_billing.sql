-- Abrechnung (Grundgerüst). Tarifkatalog liegt im Code (catalog.py, mit Quelle); die Praxis erfasst nur
-- eigene Taxpunktwerte (KVG: kantonal/vertraglich) und ggf. zusätzliche Positionen.
CREATE TABLE tax_point_values (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    tariff TEXT NOT NULL,
    value REAL NOT NULL CHECK (value > 0),
    valid_from TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (practice_id, tariff, valid_from)
);

CREATE TABLE invoices (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    insurer_id INTEGER REFERENCES insurers(id),
    prescription_id INTEGER REFERENCES prescriptions(id),
    law TEXT NOT NULL,
    tariff TEXT NOT NULL,
    tiers TEXT NOT NULL CHECK (tiers IN ('garant', 'payant')),
    number TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'final', 'cancelled')),
    case_number TEXT,
    accident_date TEXT,
    period_from TEXT,
    period_to TEXT,
    total_chf REAL NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    finalized_at TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT,
    UNIQUE (practice_id, number)
);
CREATE INDEX invoices_patient ON invoices(practice_id, patient_id);

CREATE TABLE invoice_lines (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    appointment_id INTEGER REFERENCES appointments(id),
    service_date TEXT NOT NULL,
    therapist_id INTEGER REFERENCES users(id),
    code TEXT,
    label TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 1,
    tp REAL,
    tp_value REAL,
    amount_chf REAL NOT NULL DEFAULT 0
);
CREATE INDEX invoice_lines_invoice ON invoice_lines(practice_id, invoice_id);
-- Ein Termin gilt als verrechnet, solange er auf einer nicht stornierten Rechnung steht (Prüfung im Code)
CREATE INDEX invoice_lines_appt ON invoice_lines(appointment_id);

-- Abgeschlossene Rechnungen: unveränderbar (nur Storno)
CREATE TRIGGER invoices_locked BEFORE UPDATE ON invoices
WHEN OLD.status != 'draft' AND (NEW.status = 'draft' OR NEW.total_chf IS NOT OLD.total_chf OR NEW.number IS NOT OLD.number
    OR NEW.patient_id IS NOT OLD.patient_id OR NEW.insurer_id IS NOT OLD.insurer_id OR NEW.law IS NOT OLD.law
    OR (OLD.status = 'cancelled'))
BEGIN
    SELECT RAISE(ABORT, 'Rechnung ist abgeschlossen');
END;
CREATE TRIGGER invoice_lines_locked_upd BEFORE UPDATE ON invoice_lines
WHEN (SELECT status FROM invoices WHERE id = OLD.invoice_id) != 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Rechnungszeilen sind abgeschlossen');
END;
CREATE TRIGGER invoice_lines_locked_del BEFORE DELETE ON invoice_lines
WHEN (SELECT status FROM invoices WHERE id = OLD.invoice_id) != 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Rechnungszeilen sind abgeschlossen');
END;
CREATE TRIGGER invoice_lines_locked_ins BEFORE INSERT ON invoice_lines
WHEN (SELECT status FROM invoices WHERE id = NEW.invoice_id) != 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Rechnungszeilen sind abgeschlossen');
END;
CREATE TRIGGER invoices_no_delete BEFORE DELETE ON invoices WHEN OLD.status != 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Abgeschlossene Rechnungen können nicht gelöscht werden');
END;
