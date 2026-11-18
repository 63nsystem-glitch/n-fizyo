-- Zwei-Schritt-Anmeldung (Authenticator-App) für alle Praxiskonten, strukturierte Praxisadresse
-- (für QR-Rechnung und XML-Rechnung), SMS-Anbieter der Praxis, Versand-Status im Postausgang.

-- Geheimnisse sind verschlüsselt (core/crypto.py, Schlüssel aus NFIZYO_SECRET_KEY), Wiederherstellungscodes nur als Hash.
ALTER TABLE users ADD COLUMN totp_secret TEXT;
ALTER TABLE users ADD COLUMN totp_pending TEXT;
ALTER TABLE users ADD COLUMN totp_enabled_at TEXT;
ALTER TABLE users ADD COLUMN totp_last_step INTEGER;
ALTER TABLE users ADD COLUMN recovery_codes TEXT;

-- Stufe einer Anmeldung: ok = angemeldet | code = Passwort stimmt, Code aus der App fehlt noch |
-- setup = Passwort stimmt, Zwei-Schritt-Anmeldung muss zuerst eingerichtet werden
ALTER TABLE sessions ADD COLUMN stage TEXT NOT NULL DEFAULT 'ok';

ALTER TABLE practices ADD COLUMN street TEXT;
ALTER TABLE practices ADD COLUMN house_no TEXT;
ALTER TABLE practices ADD COLUMN zip TEXT;
ALTER TABLE practices ADD COLUMN city TEXT;
-- log = nichts wird versendet (nur Postausgang) | aspsms | ecall  (Zugangsdaten nur in der .env-Datei)
ALTER TABLE practices ADD COLUMN sms_provider TEXT NOT NULL DEFAULT 'log';
-- Absender, den Patient/innen sehen (max. 11 Zeichen, nur Buchstaben/Ziffern)
ALTER TABLE practices ADD COLUMN sms_sender TEXT;

ALTER TABLE outbox ADD COLUMN provider TEXT;
ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN error TEXT;
ALTER TABLE outbox ADD COLUMN provider_ref TEXT;
ALTER TABLE outbox ADD COLUMN next_try_at TEXT;
CREATE INDEX outbox_practice ON outbox(practice_id, id);
