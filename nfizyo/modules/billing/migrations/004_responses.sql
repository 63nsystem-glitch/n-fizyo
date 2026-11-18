-- Abrechnung, Stufe 4: Antworten der Versicherer (General Invoice Response 5.0) und Kostengutsprachen.

CREATE TABLE invoice_responses (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    invoice_id INTEGER REFERENCES invoices(id),
    request_id TEXT NOT NULL,
    request_type TEXT NOT NULL DEFAULT 'invoice',
    reminder_level INTEGER,
    tiers TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('notification', 'pending', 'rejected', 'accepted')),
    status_in TEXT,
    status_out TEXT,
    explanation TEXT,
    notices TEXT,
    amount_due REAL,
    amount_paid REAL,
    insurer_gln TEXT,
    insurer_name TEXT,
    modus TEXT NOT NULL DEFAULT 'production',
    encrypted INTEGER NOT NULL DEFAULT 0,
    response_at TEXT,
    source TEXT NOT NULL CHECK (source IN ('upload', 'medidata')),
    filename TEXT,
    sha256 TEXT NOT NULL,
    xml TEXT NOT NULL,
    handled_at TEXT,
    handled_by INTEGER REFERENCES users(id),
    handled_note TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (practice_id, sha256)
);
CREATE INDEX invoice_responses_invoice ON invoice_responses(practice_id, invoice_id);
CREATE INDEX invoice_responses_open ON invoice_responses(practice_id, handled_at);

-- Kostengutsprache (z. B. KVG nach 36 Sitzungen, Art. 5 Abs. 4 KLV, oder nach Vorgabe des Versicherers)
CREATE TABLE cost_approvals (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    insurer_id INTEGER REFERENCES insurers(id),
    prescription_id INTEGER REFERENCES prescriptions(id),
    requested_on TEXT NOT NULL,
    sessions_requested INTEGER CHECK (sessions_requested IS NULL OR sessions_requested BETWEEN 1 AND 500),
    status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested', 'approved', 'partial', 'rejected')),
    sessions_approved INTEGER CHECK (sessions_approved IS NULL OR sessions_approved BETWEEN 0 AND 500),
    valid_until TEXT,
    decided_on TEXT,
    reference TEXT,
    note TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX cost_approvals_patient ON cost_approvals(practice_id, patient_id);
