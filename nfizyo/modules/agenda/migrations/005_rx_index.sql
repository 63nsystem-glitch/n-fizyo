-- 0.9.0 Leistung: Sitzungsnummern, «Verordnungen im Blick» und Serien zählen die Termine einer Verordnung.
-- Ohne Index las jede Zählung die ganze Terminliste (mit einem Jahr Daten 15–20 s pro Seite im Empfang).
CREATE INDEX appointments_prescription ON appointments(practice_id, prescription_id, status);
