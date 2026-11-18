-- 0.9.0 Zusammenführen doppelter Patient/innen (Kern-Tabelle patient_merges): Abgeschlossene Berichte bleiben unveränderbar –
-- ausser dem Wechsel der Patienten-ID während einer laufenden Zusammenführung (gleiche Person, Doppel-Eintrag).
DROP TRIGGER reports_locked;
CREATE TRIGGER reports_locked BEFORE UPDATE ON reports
WHEN OLD.status != 'draft' AND (
    NEW.status = 'draft' OR NEW.subject IS NOT OLD.subject OR NEW.salutation IS NOT OLD.salutation
    OR NEW.diagnosis IS NOT OLD.diagnosis OR NEW.course IS NOT OLD.course OR NEW.findings IS NOT OLD.findings
    OR NEW.assessment IS NOT OLD.assessment OR NEW.recommendation IS NOT OLD.recommendation
    OR NEW.lang IS NOT OLD.lang OR NEW.referrer_id IS NOT OLD.referrer_id
    OR (NEW.patient_id IS NOT OLD.patient_id AND NOT EXISTS (SELECT 1 FROM patient_merges m WHERE m.practice_id = OLD.practice_id
        AND m.drop_id = OLD.patient_id AND m.keep_id = NEW.patient_id AND m.finished_at IS NULL)))
BEGIN
    SELECT RAISE(ABORT, 'Bericht ist abgeschlossen und kann nicht mehr geändert werden');
END;
