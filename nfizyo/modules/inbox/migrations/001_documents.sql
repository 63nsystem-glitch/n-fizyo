-- Posteingang: Berichte (Upload, Scan-Ordner, später HIN), Texterkennung, KI-Vorschläge
CREATE TABLE documents (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER REFERENCES patients(id),
    filename TEXT NOT NULL,
    path TEXT,
    mime TEXT,
    size INTEGER,
    sha256 TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'upload' CHECK (source IN ('upload', 'scan', 'patient', 'hin', 'demo')),
    kind TEXT NOT NULL DEFAULT 'report' CHECK (kind IN ('report', 'surgery', 'prescription', 'imaging', 'other')),
    title TEXT,
    sender TEXT,
    doc_date TEXT,
    language TEXT,
    text TEXT,
    text_method TEXT,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'processing', 'review', 'done', 'failed')),
    extracted_name TEXT,
    extracted_birthdate TEXT,
    match_ids TEXT,
    engine TEXT,
    warnings TEXT,
    error TEXT,
    uploaded_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    processed_at TEXT,
    done_at TEXT,
    UNIQUE (practice_id, sha256)
);
CREATE INDEX documents_status ON documents(practice_id, status);
CREATE INDEX documents_patient ON documents(practice_id, patient_id);

CREATE TABLE suggestions (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    document_id INTEGER NOT NULL REFERENCES documents(id),
    patient_id INTEGER REFERENCES patients(id),
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    structure_id TEXT,
    region TEXT,
    side TEXT,
    spine_levels TEXT,
    on_date TEXT,
    until TEXT,
    sessions INTEGER,
    insurer TEXT,
    law TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'rejected', 'duplicate')),
    finding_id INTEGER,
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX suggestions_doc ON suggestions(practice_id, document_id);
CREATE INDEX suggestions_patient ON suggestions(practice_id, patient_id, status);
