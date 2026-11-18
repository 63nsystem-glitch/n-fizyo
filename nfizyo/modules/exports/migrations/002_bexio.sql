-- bexio: Buchungen aus dem Journal als «manuelle Buchungen» an bexio senden (API).
-- Zugang: Personal Access Token in der .env (NFIZYO_BEXIO_TOKEN, eine Praxis pro Box) oder OAuth (App von NSystem):
-- das Refresh-Token liegt verschlüsselt hier, nie im Klartext.
CREATE TABLE bexio_connections (
    practice_id INTEGER PRIMARY KEY REFERENCES practices(id),
    refresh_token_enc TEXT,
    pending_state TEXT,
    pending_at TEXT,
    connected_at TEXT,
    connected_by INTEGER REFERENCES users(id),
    updated_at TEXT
);

-- Jede Journalzeile höchstens einmal: Schlüssel aus Datum, Beleg, Konten, Betrag und Text.
-- status: sent = in bexio gebucht (bexio_id) · failed = abgelehnt (error), neuer Versuch möglich ·
--         unknown = keine klare Antwort (Zeitüberschreitung) → erst in bexio prüfen, dann als gebucht markieren oder neu senden
CREATE TABLE bexio_bookings (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    entry_key TEXT NOT NULL,
    day TEXT NOT NULL,
    doc TEXT NOT NULL,
    debit TEXT NOT NULL,
    credit TEXT NOT NULL,
    amount_chf REAL NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('sent', 'failed', 'unknown')),
    bexio_id INTEGER,
    error TEXT,
    created_by INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL,
    UNIQUE (practice_id, entry_key)
);
CREATE INDEX bexio_bookings_status ON bexio_bookings(practice_id, status);
