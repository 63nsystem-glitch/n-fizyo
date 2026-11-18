-- 0.9.0 Sicherheit: Inaktivität bei Praxis-Anmeldungen, SMS-Codes/Links nur bis zum Versand im Klartext.

-- Zuletzt aktiv (gleitende Abmeldung nach Inaktivität, Settings.staff_idle_hours). Bestehende Sitzungen: Start = Anlage.
ALTER TABLE sessions ADD COLUMN last_seen_at TEXT;
UPDATE sessions SET last_seen_at = created_at WHERE last_seen_at IS NULL;

-- Vollständiger SMS-Text NUR solange die SMS noch nicht versendet ist (Codes, Einladungs-/Video-/Angebotslinks).
-- outbox.body enthält immer die maskierte Fassung. Nach dem Versand (oder endgültigem Fehler) wird body_secret gelöscht.
ALTER TABLE outbox ADD COLUMN body_secret TEXT;

-- Noch nicht versendete SMS (status 'queued', z. B. wartet auf erneuten Versuch): vollen Text zuerst nach body_secret, damit
-- sie mit funktionierendem Link/Code ankommen (deliver() sendet body_secret und löscht es danach).
UPDATE outbox SET body_secret = body
    WHERE channel = 'sms' AND status = 'queued' AND body_secret IS NULL
      AND (instr(body, '/p/einladung/') > 0 OR instr(body, '/p/video/') > 0 OR instr(body, '/p/angebot/') > 0);

-- Ältere SMS (bis 0.8.0): Links mit Zugangs-Token unkenntlich machen (der Rest der SMS – bei Einladungen der Code – entfällt).
UPDATE outbox SET body = substr(body, 1, instr(body, '/p/einladung/') + 12) || '••••••'
    WHERE channel = 'sms' AND instr(body, '/p/einladung/') > 0;
UPDATE outbox SET body = substr(body, 1, instr(body, '/p/video/') + 8) || '••••••'
    WHERE channel = 'sms' AND instr(body, '/p/video/') > 0;
UPDATE outbox SET body = substr(body, 1, instr(body, '/p/angebot/') + 10) || '••••••'
    WHERE channel = 'sms' AND instr(body, '/p/angebot/') > 0;
