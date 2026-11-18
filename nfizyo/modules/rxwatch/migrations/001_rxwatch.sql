-- Verordnungen im Blick: Anfragen an Ärztinnen und Ärzte (Folgeverordnung, Kostengutsprache, Korrektur, neue Verordnung)
CREATE TABLE rx_requests (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    prescription_id INTEGER NOT NULL REFERENCES prescriptions(id),
    kind TEXT NOT NULL CHECK (kind IN ('renewal', 'kogu', 'correction', 'new')),
    via TEXT NOT NULL DEFAULT 'phone' CHECK (via IN ('phone', 'fax', 'mail', 'hin', 'report', 'other')),
    note TEXT,
    requested_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'withdrawn')),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    closed_by INTEGER REFERENCES users(id)
);
CREATE INDEX rx_requests_prescription ON rx_requests(practice_id, prescription_id, status);
CREATE INDEX rx_requests_patient ON rx_requests(practice_id, patient_id);
