-- Notizvorlagen: eigene Vorlagen der Therapeutin / des Therapeuten (owner_id) und Vorlagen der Praxis (owner_id leer, von der Leitung).
ALTER TABLE note_templates ADD COLUMN owner_id INTEGER REFERENCES users(id);
ALTER TABLE note_templates ADD COLUMN created_at TEXT;
ALTER TABLE note_templates ADD COLUMN updated_at TEXT;
CREATE INDEX note_templates_practice ON note_templates(practice_id, owner_id, lang);
