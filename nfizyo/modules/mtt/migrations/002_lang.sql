-- Sprache der Texte der Therapeutin / des Therapeuten (für die Übersetzung in der Patienten-App)
ALTER TABLE mtt_plans ADD COLUMN goal_lang TEXT NOT NULL DEFAULT 'de';
ALTER TABLE mtt_items ADD COLUMN note_lang TEXT NOT NULL DEFAULT 'de';
