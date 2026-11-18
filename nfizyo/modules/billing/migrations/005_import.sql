-- 0.9.0 Datenübernahme: offene Posten aus der bisherigen Software.
-- imported_from = Rechnungsnummer im alten System. Solche Rechnungen sind in der alten Buchhaltung schon als Ertrag gebucht
-- und erscheinen deshalb NICHT im Buchhaltungs-Export (nur die Zahlungen darauf).
ALTER TABLE invoices ADD COLUMN imported_from TEXT;
CREATE INDEX invoices_imported ON invoices(practice_id, imported_from);
