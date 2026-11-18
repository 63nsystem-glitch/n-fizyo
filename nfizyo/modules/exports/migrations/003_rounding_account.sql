-- 0.9.0 Bargeld auf 5 Rappen: Konto für Rundungsdifferenzen (Barzahlungen und Barverkäufe, core/money.py).
-- Leer = wie «Ertrag Leistungen» (acc_services). Mit der Treuhand abstimmen (oft ein eigenes Erlös- oder Aufwandkonto).
ALTER TABLE export_settings ADD COLUMN acc_rounding TEXT;
