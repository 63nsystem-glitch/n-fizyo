-- 0.9.0 Bargeld auf 5 Rappen (Schweizer Regel, core/money.py).
-- rounding_chf: bei Barverkäufen die Rundung auf 5 Rappen, die im Total enthalten ist. total_chf = was bezahlt wurde
--   (Summe der Positionen + rounding_chf); die Positionen und die MWST bleiben auf den Rappen genau.
--   Beispiel: Positionen 33.33 bar → total_chf 33.35, rounding_chf +0.02 (Quittung: «Rundung 5 Rappen +0.02»).
--   Twint, Karte, «auf Rechnung»: immer 0.
ALTER TABLE cash_sales ADD COLUMN rounding_chf REAL NOT NULL DEFAULT 0;

DROP TRIGGER cash_sales_locked;
CREATE TRIGGER cash_sales_locked BEFORE UPDATE ON cash_sales
WHEN (OLD.status = 'cancelled' AND NOT (NEW.patient_id IS NOT OLD.patient_id AND EXISTS (SELECT 1 FROM patient_merges m
        WHERE m.practice_id = OLD.practice_id AND m.drop_id = OLD.patient_id AND m.keep_id = NEW.patient_id AND m.finished_at IS NULL)))
    OR NEW.total_chf IS NOT OLD.total_chf OR NEW.number IS NOT OLD.number OR NEW.day IS NOT OLD.day
    OR NEW.method IS NOT OLD.method OR NEW.vat_chf IS NOT OLD.vat_chf OR NEW.rounding_chf IS NOT OLD.rounding_chf
    OR (NEW.patient_id IS NOT OLD.patient_id AND NOT EXISTS (SELECT 1 FROM patient_merges m WHERE m.practice_id = OLD.practice_id
        AND m.drop_id = OLD.patient_id AND m.keep_id = NEW.patient_id AND m.finished_at IS NULL))
    OR (NEW.status = 'cancelled' AND OLD.status != 'cancelled'
        AND EXISTS (SELECT 1 FROM cash_closings c WHERE c.practice_id = OLD.practice_id AND c.day = OLD.day))
BEGIN
    SELECT RAISE(ABORT, 'Quittung ist abgeschlossen');
END;
