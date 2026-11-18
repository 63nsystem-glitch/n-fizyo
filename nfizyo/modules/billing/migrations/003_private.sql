-- Abrechnung, Stufe 3: Selbstzahler und Zusatzversicherung (VVG, privat) mit eigener Preisliste der Praxis,
-- Produkte und Abos (Verkauf an der Kasse), Mehrwertsteuer (nur wenn die Praxis eine MWST-Nummer hat), Zahlungsart.

CREATE TABLE price_items (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    kind TEXT NOT NULL CHECK (kind IN ('service', 'product', 'abo')),
    code TEXT NOT NULL,
    name_de TEXT NOT NULL,
    name_fr TEXT NOT NULL,
    price_chf REAL NOT NULL CHECK (price_chf >= 0 AND price_chf <= 100000),
    vat_rate REAL NOT NULL DEFAULT 0 CHECK (vat_rate >= 0 AND vat_rate < 30),
    treatment_type_id INTEGER,           -- Leistung: Preis für Termine dieser Behandlungsart (leer = alle übrigen)
    abo_sessions INTEGER CHECK (abo_sessions IS NULL OR abo_sessions BETWEEN 1 AND 500),   -- leer = unbegrenzt
    abo_days INTEGER CHECK (abo_days IS NULL OR abo_days BETWEEN 1 AND 1100),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    UNIQUE (practice_id, code)
);
CREATE INDEX price_items_practice ON price_items(practice_id, kind, active);

-- UID der Praxis (CHE-123.456.789), leer = nicht MWST-pflichtig
ALTER TABLE billing_settings ADD COLUMN vat_number TEXT;

ALTER TABLE invoices ADD COLUMN vat_chf REAL NOT NULL DEFAULT 0;
-- beim Abschliessen festgehalten
ALTER TABLE invoices ADD COLUMN vat_number TEXT;

-- Preisliste (Selbstzahler); Tarif-Zeilen: leer
ALTER TABLE invoice_lines ADD COLUMN unit_price REAL;
ALTER TABLE invoice_lines ADD COLUMN vat_rate REAL NOT NULL DEFAULT 0;
ALTER TABLE invoice_lines ADD COLUMN price_item_id INTEGER;

ALTER TABLE invoice_payments ADD COLUMN method TEXT NOT NULL DEFAULT 'bank'
    CHECK (method IN ('bank', 'cash', 'twint', 'card'));

CREATE TRIGGER invoices_vat_locked BEFORE UPDATE ON invoices
WHEN OLD.status != 'draft' AND (NEW.vat_chf IS NOT OLD.vat_chf OR NEW.vat_number IS NOT OLD.vat_number OR NEW.tariff IS NOT OLD.tariff)
BEGIN
    SELECT RAISE(ABORT, 'Rechnung ist abgeschlossen');
END;
