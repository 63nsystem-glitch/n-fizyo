-- Berichte an Zuweisende (Verlauf, Gesuch um Folgeverordnung, Abschluss).
-- Entwurf → abgeschlossen (unveränderbar) → als versendet markiert.
CREATE TABLE reports (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    referrer_id INTEGER REFERENCES referrers(id),
    prescription_id INTEGER REFERENCES prescriptions(id),
    kind TEXT NOT NULL CHECK (kind IN ('progress', 'renewal', 'discharge')),
    lang TEXT NOT NULL DEFAULT 'de',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'final', 'sent')),
    subject TEXT NOT NULL,
    salutation TEXT NOT NULL,
    diagnosis TEXT NOT NULL DEFAULT '',
    course TEXT NOT NULL DEFAULT '',
    findings TEXT NOT NULL DEFAULT '',
    assessment TEXT NOT NULL DEFAULT '',
    recommendation TEXT NOT NULL DEFAULT '',
    author_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finalized_at TEXT,
    finalized_by INTEGER REFERENCES users(id),
    sent_at TEXT,
    sent_via TEXT
);
CREATE INDEX reports_patient ON reports(practice_id, patient_id);

-- Abgeschlossene Berichte: Inhalt unveränderbar, nur "versendet" darf gesetzt werden.
CREATE TRIGGER reports_locked BEFORE UPDATE ON reports
WHEN OLD.status != 'draft' AND (
    NEW.status = 'draft' OR NEW.subject IS NOT OLD.subject OR NEW.salutation IS NOT OLD.salutation
    OR NEW.diagnosis IS NOT OLD.diagnosis OR NEW.course IS NOT OLD.course OR NEW.findings IS NOT OLD.findings
    OR NEW.assessment IS NOT OLD.assessment OR NEW.recommendation IS NOT OLD.recommendation
    OR NEW.lang IS NOT OLD.lang OR NEW.referrer_id IS NOT OLD.referrer_id OR NEW.patient_id IS NOT OLD.patient_id)
BEGIN
    SELECT RAISE(ABORT, 'Bericht ist abgeschlossen und kann nicht mehr geändert werden');
END;

CREATE TRIGGER reports_no_delete BEFORE DELETE ON reports WHEN OLD.status != 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Abgeschlossene Berichte können nicht gelöscht werden');
END;
