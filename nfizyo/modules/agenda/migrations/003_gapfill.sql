-- Lücke füllen: frei gewordener Termin wird Personen auf der Warteliste per SMS angeboten.
-- Wer zuerst bestätigt, bekommt den Termin; die anderen erfahren, dass er vergeben ist.
CREATE TABLE slot_offers (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),   -- der abgesagte Termin (Quelle der Lücke)
    therapist_id INTEGER NOT NULL REFERENCES users(id),
    location_id INTEGER REFERENCES locations(id),
    room_id INTEGER REFERENCES rooms(id),
    device_id INTEGER REFERENCES devices(id),
    treatment_type_id INTEGER REFERENCES treatment_types(id),
    starts_at TEXT NOT NULL,                                       -- UTC
    ends_at TEXT NOT NULL,                                         -- UTC
    expires_at TEXT NOT NULL,                                      -- UTC
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'taken', 'closed', 'expired')),
    booked_appointment_id INTEGER REFERENCES appointments(id),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX slot_offers_appt ON slot_offers(practice_id, appointment_id, status);

CREATE TABLE slot_offer_recipients (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    offer_id INTEGER NOT NULL REFERENCES slot_offers(id),
    waitlist_id INTEGER NOT NULL REFERENCES waitlist(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    phone TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'de',
    token_hash TEXT NOT NULL UNIQUE,                               -- nur der Hash; der Link steht allein in der SMS
    status TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'accepted', 'declined', 'too_late')),
    sent_at TEXT NOT NULL,
    answered_at TEXT,
    UNIQUE (offer_id, waitlist_id)
);
CREATE INDEX slot_offer_recipients_offer ON slot_offer_recipients(practice_id, offer_id);
