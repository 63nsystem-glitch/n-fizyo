-- E-Mail an Patient/innen über den Postausgang (Betreff, Anhang bis zum Versand) und Einverständnis/Kanal der Patient/innen.

ALTER TABLE outbox ADD COLUMN subject TEXT;
ALTER TABLE outbox ADD COLUMN attachment_name TEXT;
-- PDF-Anhang nur bis zum Versand (danach gelöscht)
ALTER TABLE outbox ADD COLUMN attachment BLOB;

-- E-Mail ist unverschlüsselt: nur mit ausdrücklichem Einverständnis (Datum)
ALTER TABLE patients ADD COLUMN email_ok_at TEXT;
-- Erinnerungen: sms | email | both | none
ALTER TABLE patients ADD COLUMN reminder_channel TEXT NOT NULL DEFAULT 'sms'
    CHECK (reminder_channel IN ('sms', 'email', 'both', 'none'));
