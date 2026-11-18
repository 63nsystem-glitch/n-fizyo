-- 0.9.0 Empfang und Abrechnung: doppelte Patient/innen zusammenführen, Taxpunktwert je Versicherergruppe.

-- Zusammenführen (nur Leitung): Der Doppel-Eintrag (drop_id) geht im behaltenen Eintrag (keep_id) auf. Solange die Zeile
-- offen ist (finished_at leer), erlauben die Sperren der Module (abgeschlossene Rechnungen, Berichte, Quittungen) genau
-- diesen einen Wechsel der Patienten-ID – sonst bleiben sie unveränderbar. Keine Fremdschlüssel: der Doppel-Eintrag
-- existiert danach nicht mehr. summary = JSON mit den verschobenen Einträgen je Modul.
CREATE TABLE patient_merges (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    drop_id INTEGER NOT NULL,
    keep_id INTEGER NOT NULL,
    drop_number TEXT,
    summary TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX patient_merges_open ON patient_merges(practice_id, drop_id, finished_at);

-- Gruppe des Versicherers für den KVG-Taxpunktwert (Einkaufsgemeinschaft / Vertrag):
-- tarifsuisse | hsk | css | other – leer = Wert der Praxis (Leitung › Einstellungen › Tarife).
ALTER TABLE insurers ADD COLUMN tp_group TEXT;
