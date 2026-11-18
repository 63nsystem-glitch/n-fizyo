-- Kasse an der Theke: Verkauf von Produkten, Abos und Leistungen (bar, Twint, Karte oder auf Rechnung),
-- Quittungen, Abos mit Besuchen, Tagesabschluss. Preise kommen aus der Preisliste der Abrechnung.

CREATE TABLE cash_closings (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    day TEXT NOT NULL,
    cash_expected REAL NOT NULL,
    cash_counted REAL NOT NULL,
    difference REAL NOT NULL,
    twint_total REAL NOT NULL DEFAULT 0,
    card_total REAL NOT NULL DEFAULT 0,
    sales INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (practice_id, day)
);

CREATE TABLE cash_sales (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    number TEXT NOT NULL,
    day TEXT NOT NULL,
    patient_id INTEGER REFERENCES patients(id),
    method TEXT NOT NULL CHECK (method IN ('cash', 'twint', 'card', 'invoice')),
    total_chf REAL NOT NULL,
    vat_chf REAL NOT NULL DEFAULT 0,
    vat_number TEXT,
    given_chf REAL,
    invoice_id INTEGER,
    status TEXT NOT NULL DEFAULT 'paid' CHECK (status IN ('paid', 'invoiced', 'cancelled')),
    cancelled_at TEXT,
    cancel_reason TEXT,
    cancelled_by INTEGER REFERENCES users(id),
    note TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (practice_id, number)
);
CREATE INDEX cash_sales_day ON cash_sales(practice_id, day);
CREATE INDEX cash_sales_patient ON cash_sales(practice_id, patient_id);

CREATE TABLE cash_sale_lines (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    sale_id INTEGER NOT NULL REFERENCES cash_sales(id),
    price_item_id INTEGER,
    kind TEXT NOT NULL,
    code TEXT NOT NULL,
    label TEXT NOT NULL,
    quantity REAL NOT NULL CHECK (quantity > 0),
    unit_price REAL NOT NULL,
    vat_rate REAL NOT NULL DEFAULT 0,
    amount_chf REAL NOT NULL
);
CREATE INDEX cash_sale_lines_sale ON cash_sale_lines(practice_id, sale_id);

CREATE TABLE subscriptions (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    sale_id INTEGER REFERENCES cash_sales(id),
    price_item_id INTEGER,
    label TEXT NOT NULL,
    sessions_total INTEGER,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE INDEX subscriptions_patient ON subscriptions(practice_id, patient_id);

CREATE TABLE subscription_visits (
    id INTEGER PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id),
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    visited_on TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (subscription_id, visited_on)
);

-- Quittungen sind unveränderbar: nur Storno (vor dem Tagesabschluss)
CREATE TRIGGER cash_sales_locked BEFORE UPDATE ON cash_sales
WHEN OLD.status = 'cancelled' OR NEW.total_chf IS NOT OLD.total_chf OR NEW.number IS NOT OLD.number OR NEW.day IS NOT OLD.day
    OR NEW.method IS NOT OLD.method OR NEW.patient_id IS NOT OLD.patient_id OR NEW.vat_chf IS NOT OLD.vat_chf
    OR (NEW.status = 'cancelled' AND EXISTS (SELECT 1 FROM cash_closings c WHERE c.practice_id = OLD.practice_id AND c.day = OLD.day))
BEGIN
    SELECT RAISE(ABORT, 'Quittung ist abgeschlossen');
END;
CREATE TRIGGER cash_sales_no_delete BEFORE DELETE ON cash_sales
BEGIN
    SELECT RAISE(ABORT, 'Quittungen werden nicht gelöscht (nur Storno)');
END;
CREATE TRIGGER cash_sale_lines_locked_upd BEFORE UPDATE ON cash_sale_lines
BEGIN
    SELECT RAISE(ABORT, 'Quittungszeilen sind unveränderbar');
END;
CREATE TRIGGER cash_sale_lines_locked_del BEFORE DELETE ON cash_sale_lines
BEGIN
    SELECT RAISE(ABORT, 'Quittungszeilen sind unveränderbar');
END;
CREATE TRIGGER cash_closings_locked BEFORE UPDATE ON cash_closings
BEGIN
    SELECT RAISE(ABORT, 'Tagesabschluss ist unveränderbar');
END;
CREATE TRIGGER cash_closings_no_delete BEFORE DELETE ON cash_closings
BEGIN
    SELECT RAISE(ABORT, 'Tagesabschluss ist unveränderbar');
END;
-- Nach dem Tagesabschluss keine neuen Verkäufe für diesen Tag
CREATE TRIGGER cash_sales_closed_day BEFORE INSERT ON cash_sales
WHEN EXISTS (SELECT 1 FROM cash_closings c WHERE c.practice_id = NEW.practice_id AND c.day = NEW.day)
BEGIN
    SELECT RAISE(ABORT, 'Tag ist abgeschlossen');
END;
