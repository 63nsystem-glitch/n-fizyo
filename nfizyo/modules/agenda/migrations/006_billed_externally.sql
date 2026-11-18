-- 0.9.0 Datenübernahme: Vergangene, erledigte Termine aus der bisherigen Software sind dort meist schon verrechnet.
-- billed_externally = 1 → erscheinen nicht unter Abrechnung › «Bereit» (keine doppelte Rechnung), zählen aber weiter als
-- Sitzungen (Verordnung, «erste Behandlung», Statistik).
ALTER TABLE appointments ADD COLUMN billed_externally INTEGER NOT NULL DEFAULT 0;
