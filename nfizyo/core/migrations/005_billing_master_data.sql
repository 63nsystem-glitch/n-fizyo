-- Stammdaten für die elektronische Rechnung (XML 5.0): AHV-Nummer der Patientin / des Patienten (Pflicht im XML),
-- Adresse der Versicherer (Pflicht im XML).
ALTER TABLE patients ADD COLUMN ahv_number TEXT;
ALTER TABLE insurers ADD COLUMN street TEXT;
ALTER TABLE insurers ADD COLUMN zip TEXT;
ALTER TABLE insurers ADD COLUMN city TEXT;
