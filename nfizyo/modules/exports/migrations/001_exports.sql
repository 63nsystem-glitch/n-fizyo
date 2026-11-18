-- Export für die Buchhaltung: Kontenplan der Praxis (Vorgabe: Schweizer KMU-Kontenrahmen, mit der Treuhand abstimmen).
CREATE TABLE export_settings (
    practice_id INTEGER PRIMARY KEY REFERENCES practices(id),
    acc_receivable TEXT NOT NULL DEFAULT '1100',
    acc_bank TEXT NOT NULL DEFAULT '1020',
    acc_cash TEXT NOT NULL DEFAULT '1000',
    acc_transit TEXT NOT NULL DEFAULT '1091',
    acc_services TEXT NOT NULL DEFAULT '3400',
    acc_products TEXT NOT NULL DEFAULT '3200',
    acc_fees TEXT NOT NULL DEFAULT '3600',
    acc_vat TEXT NOT NULL DEFAULT '2200',
    acc_unassigned TEXT NOT NULL DEFAULT '1100',
    updated_at TEXT
);
