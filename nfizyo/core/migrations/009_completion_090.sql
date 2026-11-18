-- 0.9.0 Vervollständigung: Datenübernahme von Verordnungen, Antwortzeit der Praxis in der Patienten-App.

-- Sitzungen, die schon in der bisherigen Software durchgeführt wurden (Übernahme). Zählt zu den verbrauchten Sitzungen.
ALTER TABLE prescriptions ADD COLUMN sessions_before INTEGER NOT NULL DEFAULT 0;

-- Antwortzeit der Praxis in der Patienten-App (Auswahl, in 11 Sprachen übersetzt). NULL = Standardtext.
-- Werte: same_day | next_day | two_days | three_days | week
ALTER TABLE practices ADD COLUMN reply_time TEXT;
