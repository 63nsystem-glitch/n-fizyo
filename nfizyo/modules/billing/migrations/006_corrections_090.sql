-- 0.9.0 Zahlungen korrigieren, MediData «unklar», Taxpunktwert je Versicherergruppe, Zusammenführen doppelter Patient/innen.

-- ---------------------------------------------------------------- Zahlungen
-- kind: payment (Eingang) | refund (Rückzahlung, Betrag negativ) | reversal (Storno einer Buchung, Gegenbuchung, negativ).
-- Gebuchte Zahlungen werden nie gelöscht und nie geändert – korrigiert wird mit einer Gegenbuchung.
-- dismissed_*: Bankzeile ohne Zuordnung ausgeblendet (z. B. keine Patientenzahlung) – mit Grund, rückgängig machbar.
ALTER TABLE invoice_payments ADD COLUMN kind TEXT NOT NULL DEFAULT 'payment' CHECK (kind IN ('payment', 'refund', 'reversal'));
ALTER TABLE invoice_payments ADD COLUMN reversal_of INTEGER REFERENCES invoice_payments(id);
ALTER TABLE invoice_payments ADD COLUMN dismissed_at TEXT;
ALTER TABLE invoice_payments ADD COLUMN dismissed_by INTEGER REFERENCES users(id);
ALTER TABLE invoice_payments ADD COLUMN dismiss_reason TEXT;
CREATE UNIQUE INDEX invoice_payments_reversal ON invoice_payments(reversal_of) WHERE reversal_of IS NOT NULL;

CREATE TRIGGER invoice_payments_no_delete BEFORE DELETE ON invoice_payments
BEGIN
    SELECT RAISE(ABORT, 'Zahlungen werden nicht gelöscht (nur Storno)');
END;
CREATE TRIGGER invoice_payments_locked BEFORE UPDATE ON invoice_payments
WHEN NEW.amount_chf IS NOT OLD.amount_chf OR NEW.booked_on IS NOT OLD.booked_on OR NEW.method IS NOT OLD.method
    OR NEW.source IS NOT OLD.source OR NEW.kind IS NOT OLD.kind OR NEW.reversal_of IS NOT OLD.reversal_of
BEGIN
    SELECT RAISE(ABORT, 'Gebuchte Zahlungen sind unveränderbar (nur Storno)');
END;

-- Zuordnung geändert (zugeordnet / aufgehoben / andere Rechnung): für das Buchungsjournal (Umbuchung am Tag der Änderung).
CREATE TABLE payment_moves (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    payment_id INTEGER NOT NULL REFERENCES invoice_payments(id),
    from_invoice_id INTEGER REFERENCES invoices(id),
    to_invoice_id INTEGER REFERENCES invoices(id),
    moved_on TEXT NOT NULL,
    reason TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE INDEX payment_moves_payment ON payment_moves(practice_id, payment_id);
CREATE TRIGGER payment_moves_locked BEFORE UPDATE ON payment_moves
BEGIN
    SELECT RAISE(ABORT, 'Umbuchungen sind unveränderbar');
END;
CREATE TRIGGER payment_moves_no_delete BEFORE DELETE ON payment_moves
BEGIN
    SELECT RAISE(ABORT, 'Umbuchungen sind unveränderbar');
END;

-- ---------------------------------------------------------------- MediData: Ergebnis «unklar» (Zeitüberschreitung, 5xx)
-- Neue Tabelle, weil SQLite eine CHECK-Bedingung nicht ändern kann. checked_*: wer den Stand von Hand geklärt hat.
ALTER TABLE invoice_transmissions RENAME TO invoice_transmissions_v1;
CREATE TABLE invoice_transmissions (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    kind TEXT NOT NULL CHECK (kind IN ('invoice', 'copy', 'reminder', 'storno')),
    reminder_level INTEGER,
    guid TEXT NOT NULL,
    modus TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('file', 'medidata')),
    status TEXT NOT NULL CHECK (status IN ('created', 'sent', 'failed', 'unknown')),
    reference TEXT,
    error TEXT,
    xml TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    sent_at TEXT,
    checked_by INTEGER REFERENCES users(id),
    checked_at TEXT
);
INSERT INTO invoice_transmissions (id, practice_id, invoice_id, kind, reminder_level, guid, modus, channel, status, reference, error, xml,
                                   created_by, created_at, sent_at)
    SELECT id, practice_id, invoice_id, kind, reminder_level, guid, modus, channel, status, reference, error, xml, created_by, created_at,
           sent_at FROM invoice_transmissions_v1;
DROP TABLE invoice_transmissions_v1;
CREATE INDEX invoice_transmissions_invoice ON invoice_transmissions(practice_id, invoice_id);

-- ---------------------------------------------------------------- Taxpunktwert je Versicherergruppe
-- Der KVG-Taxpunktwert der Physiotherapie gilt je Kanton und Einkaufsgemeinschaft der Versicherer (tarifsuisse, HSK, CSS).
-- insurer_group leer = Wert der Praxis (für alle Versicherer ohne eigene Gruppe).
ALTER TABLE tax_point_values RENAME TO tax_point_values_v1;
CREATE TABLE tax_point_values (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    tariff TEXT NOT NULL,
    insurer_group TEXT NOT NULL DEFAULT '' CHECK (insurer_group IN ('', 'tarifsuisse', 'hsk', 'css', 'other')),
    value REAL NOT NULL CHECK (value > 0),
    valid_from TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (practice_id, tariff, insurer_group, valid_from)
);
INSERT INTO tax_point_values (id, practice_id, tariff, insurer_group, value, valid_from, note, created_at)
    SELECT id, practice_id, tariff, '', value, valid_from, note, created_at FROM tax_point_values_v1;
DROP TABLE tax_point_values_v1;

-- ---------------------------------------------------------------- Zusammenführen doppelter Patient/innen
-- Abgeschlossene und stornierte Rechnungen bleiben unveränderbar – ausser dem Wechsel der Patienten-ID während einer
-- laufenden Zusammenführung (Kern-Tabelle patient_merges).
DROP TRIGGER invoices_locked;
CREATE TRIGGER invoices_locked BEFORE UPDATE ON invoices
WHEN OLD.status != 'draft' AND (NEW.status = 'draft' OR NEW.total_chf IS NOT OLD.total_chf OR NEW.number IS NOT OLD.number
    OR (NEW.patient_id IS NOT OLD.patient_id AND NOT EXISTS (SELECT 1 FROM patient_merges m WHERE m.practice_id = OLD.practice_id
        AND m.drop_id = OLD.patient_id AND m.keep_id = NEW.patient_id AND m.finished_at IS NULL))
    OR NEW.insurer_id IS NOT OLD.insurer_id OR NEW.law IS NOT OLD.law
    OR (OLD.status = 'cancelled' AND NOT (NEW.patient_id IS NOT OLD.patient_id AND EXISTS (SELECT 1 FROM patient_merges m
        WHERE m.practice_id = OLD.practice_id AND m.drop_id = OLD.patient_id AND m.keep_id = NEW.patient_id AND m.finished_at IS NULL))))
BEGIN
    SELECT RAISE(ABORT, 'Rechnung ist abgeschlossen');
END;
