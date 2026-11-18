-- 0.9.0 Bargeld auf 5 Rappen und Mitteilung der Bank.
--
-- rounding_chf: Barzahlung auf 5 Rappen gerundet (Schweizer Regel, core/money.py). amount_chf bleibt der Betrag, der der
--   Rechnung gutgeschrieben wird (auf den Rappen genau); rounding_chf ist die Differenz zum Bargeld, das wirklich in die Kasse
--   ging (Bargeld = amount_chf + rounding_chf, höchstens ±0.04). Bank, QR, Twint, Karte: immer 0.
--   Beispiel: Rechnung CHF 186.48 bar bezahlt → amount_chf 186.48, rounding_chf +0.02, in der Kasse 186.50; die Rechnung ist bezahlt.
-- message: Mitteilung der zahlenden Person aus der Bankdatei (camt RmtInf/Ustrd, AddtlRmtInf) – z. B. «Rechnung 2026-0099»,
--   wenn ohne QR-Referenz bezahlt wurde. Wird bei «Ohne Zuordnung» angezeigt.
ALTER TABLE invoice_payments ADD COLUMN rounding_chf REAL NOT NULL DEFAULT 0;
ALTER TABLE invoice_payments ADD COLUMN message TEXT;

DROP TRIGGER invoice_payments_locked;
CREATE TRIGGER invoice_payments_locked BEFORE UPDATE ON invoice_payments
WHEN NEW.amount_chf IS NOT OLD.amount_chf OR NEW.booked_on IS NOT OLD.booked_on OR NEW.method IS NOT OLD.method
    OR NEW.source IS NOT OLD.source OR NEW.kind IS NOT OLD.kind OR NEW.reversal_of IS NOT OLD.reversal_of
    OR NEW.rounding_chf IS NOT OLD.rounding_chf
BEGIN
    SELECT RAISE(ABORT, 'Gebuchte Zahlungen sind unveränderbar (nur Storno)');
END;
