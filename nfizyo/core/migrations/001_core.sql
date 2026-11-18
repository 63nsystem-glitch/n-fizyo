-- Kern: Praxen, Benutzer, Sitzungen, Audit, Postausgang

CREATE TABLE practices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    default_lang TEXT NOT NULL DEFAULT 'de',
    is_demo INTEGER NOT NULL DEFAULT 0,
    phone TEXT,
    email TEXT,
    address TEXT,
    canton TEXT NOT NULL DEFAULT 'BE',
    gln TEXT,
    zsr TEXT,
    interpreter_hint_de TEXT NOT NULL DEFAULT 'Für Aufklärung und wichtige Entscheide eine professionelle Dolmetscherin / einen professionellen Dolmetscher beiziehen.',
    interpreter_hint_fr TEXT NOT NULL DEFAULT 'Pour l''information du patient et les décisions importantes, faire appel à une interprète / un interprète professionnel.',
    reply_time_de TEXT NOT NULL DEFAULT 'Eine Antwort erhalten Sie in der Regel bis zum nächsten Arbeitstag.',
    reply_time_fr TEXT NOT NULL DEFAULT 'Vous recevez en général une réponse le jour ouvrable suivant.',
    created_at TEXT NOT NULL
);

CREATE TABLE practice_modules (
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    module TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    PRIMARY KEY (practice_id, module)
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    initials TEXT NOT NULL,
    -- Rollen, kommagetrennt: owner (Leitung), therapist (Therapie), reception (Empfang). Eine Person kann mehrere haben.
    roles TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'de',
    password_hash TEXT NOT NULL,
    gln TEXT,
    zsr TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX users_practice ON users(practice_id);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('staff', 'patient')),
    user_id INTEGER REFERENCES users(id),
    patient_id INTEGER,
    practice_id INTEGER REFERENCES practices(id),
    csrf TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX sessions_user ON sessions(user_id);

-- Wer hat wann was eingegeben, geändert oder angesehen (PatV BE Art. 4). Nur anhängen.
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER,
    user_id INTEGER,
    patient_account INTEGER,
    patient_id INTEGER,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    detail TEXT,
    at TEXT NOT NULL
);
CREATE INDEX audit_patient ON audit_log(practice_id, patient_id);

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log ist nur anhängbar');
END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log ist nur anhängbar');
END;

-- SMS / E-Mail. Mit NFIZYO_SMS=log wird nichts versendet, nur hier abgelegt.
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER REFERENCES practices(id),
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL,
    sent_at TEXT
);
