-- 0.9.0 Korrektur nach dem Tagesabschluss (nur Leitung): Der abgeschlossene Tag bleibt unverändert, die Korrektur wird
-- als eigene Buchung angefügt (nur Einfügen, nie ändern oder löschen), mit Grund, und im Tagesbericht gezeigt.
--   storno  Quittung eines abgeschlossenen Tages storniert. refund_day = Tag, an dem Geld zurückgegeben wurde
--           (zählt dann in der Kasse dieses Tages); leer = Buchungsfehler, kein Geld bewegt.
--   method  Falsche Zahlungsart gebucht (z. B. bar statt Karte): Betrag von «method» nach «to_method».
CREATE TABLE cash_corrections (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    day TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('storno', 'method')),
    sale_id INTEGER NOT NULL REFERENCES cash_sales(id),
    method TEXT NOT NULL CHECK (method IN ('cash', 'twint', 'card', 'invoice')),
    to_method TEXT CHECK (to_method IS NULL OR to_method IN ('cash', 'twint', 'card')),
    amount_chf REAL NOT NULL CHECK (amount_chf > 0),
    refund_day TEXT,
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    CHECK ((kind = 'storno' AND to_method IS NULL) OR (kind = 'method' AND to_method IS NOT NULL AND to_method != method AND refund_day IS NULL))
);
CREATE UNIQUE INDEX cash_corrections_storno ON cash_corrections(practice_id, sale_id) WHERE kind = 'storno';
CREATE INDEX cash_corrections_day ON cash_corrections(practice_id, day);
CREATE INDEX cash_corrections_refund ON cash_corrections(practice_id, refund_day);

CREATE TRIGGER cash_corrections_locked BEFORE UPDATE ON cash_corrections
BEGIN
    SELECT RAISE(ABORT, 'Korrekturen sind unveränderbar');
END;
CREATE TRIGGER cash_corrections_no_delete BEFORE DELETE ON cash_corrections
BEGIN
    SELECT RAISE(ABORT, 'Korrekturen sind unveränderbar');
END;
-- Nur für abgeschlossene Tage (vorher: normales Storno); Geld zurück nur an einem offenen Tag.
CREATE TRIGGER cash_corrections_closed_only BEFORE INSERT ON cash_corrections
WHEN NOT EXISTS (SELECT 1 FROM cash_closings c WHERE c.practice_id = NEW.practice_id AND c.day = NEW.day)
    OR (NEW.refund_day IS NOT NULL AND EXISTS (SELECT 1 FROM cash_closings c WHERE c.practice_id = NEW.practice_id AND c.day = NEW.refund_day))
BEGIN
    SELECT RAISE(ABORT, 'Korrektur nur für abgeschlossene Tage');
END;

-- Zusammenführen doppelter Patient/innen (Kern-Tabelle patient_merges): Quittungen bleiben unveränderbar – ausser dem
-- Wechsel der Patienten-ID während einer laufenden Zusammenführung.
DROP TRIGGER cash_sales_locked;
CREATE TRIGGER cash_sales_locked BEFORE UPDATE ON cash_sales
WHEN (OLD.status = 'cancelled' AND NOT (NEW.patient_id IS NOT OLD.patient_id AND EXISTS (SELECT 1 FROM patient_merges m
        WHERE m.practice_id = OLD.practice_id AND m.drop_id = OLD.patient_id AND m.keep_id = NEW.patient_id AND m.finished_at IS NULL)))
    OR NEW.total_chf IS NOT OLD.total_chf OR NEW.number IS NOT OLD.number OR NEW.day IS NOT OLD.day
    OR NEW.method IS NOT OLD.method OR NEW.vat_chf IS NOT OLD.vat_chf
    OR (NEW.patient_id IS NOT OLD.patient_id AND NOT EXISTS (SELECT 1 FROM patient_merges m WHERE m.practice_id = OLD.practice_id
        AND m.drop_id = OLD.patient_id AND m.keep_id = NEW.patient_id AND m.finished_at IS NULL))
    OR (NEW.status = 'cancelled' AND OLD.status != 'cancelled'
        AND EXISTS (SELECT 1 FROM cash_closings c WHERE c.practice_id = OLD.practice_id AND c.day = OLD.day))
BEGIN
    SELECT RAISE(ABORT, 'Quittung ist abgeschlossen');
END;
