-- 0.9.0 Patienten-ID und Patientennummer werden NIE wiederverwendet (auch nicht nach Zusammenführen oder Löschen).
-- Früher: Nummer = Anzahl + 1, ID = grösste ID + 1 – nach dem Zusammenführen bekam die nächste neue Person die ID und die
-- Nummer der gelöschten (Protokoll, alte Briefe und patient_merges zeigten dann auf eine andere Person).
--
-- patient_id_last:      grösste je vergebene Patienten-ID (die grösste über alle Praxen ist die Grenze – IDs sind global).
-- patient_number_last:  grösste je vergebene Nummer im Format P00001 (P + mindestens 5 Ziffern) dieser Praxis.
-- Neue Patient/innen bekommen ID und Nummer darüber (core/patients.py create / next_number). Der Trigger merkt sich jede
-- neue ID/Nummer und verweigert eine ID, die schon einmal vergeben war.
ALTER TABLE practices ADD COLUMN patient_id_last INTEGER NOT NULL DEFAULT 0;
ALTER TABLE practices ADD COLUMN patient_number_last INTEGER NOT NULL DEFAULT 0;

-- Bestehende Daten (Update von 0.8.0): höchste bekannte ID / Nummer – auch von schon zusammengeführten oder gelöschten Einträgen
-- (patient_merges, Protokoll).
UPDATE practices SET patient_id_last = MAX(
    COALESCE((SELECT MAX(id) FROM patients WHERE practice_id = practices.id), 0),
    COALESCE((SELECT MAX(drop_id) FROM patient_merges WHERE practice_id = practices.id), 0),
    COALESCE((SELECT MAX(patient_id) FROM audit_log WHERE practice_id = practices.id), 0),
    COALESCE((SELECT MAX(entity_id) FROM audit_log WHERE practice_id = practices.id AND entity = 'patient'), 0));

UPDATE practices SET patient_number_last = MAX(
    COALESCE((SELECT MAX(CAST(substr(number, 2) AS INTEGER)) FROM patients WHERE practice_id = practices.id
              AND number GLOB 'P[0-9][0-9][0-9][0-9][0-9]*' AND NOT substr(number, 2) GLOB '*[^0-9]*'), 0),
    COALESCE((SELECT MAX(CAST(substr(drop_number, 2) AS INTEGER)) FROM patient_merges WHERE practice_id = practices.id
              AND drop_number GLOB 'P[0-9][0-9][0-9][0-9][0-9]*' AND NOT substr(drop_number, 2) GLOB '*[^0-9]*'), 0),
    COALESCE((SELECT MAX(CAST(substr(json_extract(detail, '$.number'), 2) AS INTEGER)) FROM audit_log
              WHERE practice_id = practices.id AND entity = 'patient' AND action = 'delete' AND json_valid(detail)
              AND json_extract(detail, '$.number') GLOB 'P[0-9][0-9][0-9][0-9][0-9]*'
              AND NOT substr(json_extract(detail, '$.number'), 2) GLOB '*[^0-9]*'), 0));

CREATE TRIGGER patients_id_never_reused AFTER INSERT ON patients
BEGIN
    SELECT RAISE(ABORT, 'Patienten-ID war schon vergeben – IDs werden nie wiederverwendet')
     WHERE NEW.id <= (SELECT COALESCE(MAX(patient_id_last), 0) FROM practices);
    UPDATE practices SET patient_id_last = NEW.id,
        patient_number_last = MAX(patient_number_last,
            CASE WHEN NEW.number GLOB 'P[0-9][0-9][0-9][0-9][0-9]*' AND NOT substr(NEW.number, 2) GLOB '*[^0-9]*'
                 THEN CAST(substr(NEW.number, 2) AS INTEGER) ELSE 0 END)
     WHERE id = NEW.practice_id;
END;
