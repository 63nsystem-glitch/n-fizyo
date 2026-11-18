"""Unveränderbarkeit: freigegebene Notizen, Audit-Protokoll, abgeschlossene Berichte und Rechnungen."""
from __future__ import annotations

import sqlite3

import pytest
from conftest import login, pid_of

from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.modules.documentation import repo as notes


def test_approved_note_is_locked(app):
    db = app[DB]
    with practice_scope(1, 2):
        nid = notes.create(db, pid_of(app, "Lea"), {"befund": "Flexion 100°"})
        notes.approve(db, nid)
        with pytest.raises(notes.NoteLocked):
            notes.save(db, nid, {"befund": "geändert"})
        assert notes.save(db, nid, {"befund": "Flexion 100°, Nachtrag: Erguss"}, reason="Nachtrag") == 2
        assert notes.discard_draft(db, nid) is False
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE note_versions SET befund = 'x' WHERE note_id = ?", (nid,))
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM note_versions WHERE note_id = ?", (nid,))
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM notes WHERE id = ?", (nid,))
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE notes SET status = 'draft' WHERE id = ?", (nid,))


def test_audit_log_is_append_only(app):
    db = app[DB]
    assert db.value("SELECT COUNT(*) FROM audit_log") > 0
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE audit_log SET action = 'x'")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM audit_log")


async def test_report_lock(client, app):
    db = app[DB]
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    peter = pid_of(app, "Peter")
    r = await client.post(f"/therapie/patienten/{peter}/berichte", data={"_csrf": csrf, "kind": "renewal"}, allow_redirects=False)
    rid = int(r.headers["Location"].split("/")[3].split("?")[0])
    row = db.one("SELECT * FROM reports WHERE id = ?", (rid,))
    assert "Folgeverordnung" in row["recommendation"] and "Herrn Peter Muster" in row["course"]
    await client.post(f"/therapie/berichte/{rid}", data={"_csrf": csrf, "assessment": "Geprüft.", "do": "finalize"})
    assert db.value("SELECT status FROM reports WHERE id = ?", (rid,)) == "final"
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE reports SET assessment = 'x' WHERE id = ?", (rid,))
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM reports WHERE id = ?", (rid,))
    await client.post(f"/therapie/berichte/{rid}", data={"_csrf": csrf, "assessment": "Hack"})
    assert db.value("SELECT assessment FROM reports WHERE id = ?", (rid,)) == "Geprüft."


async def test_invoice_flow_and_lock(client, app):
    """Empfang bereitet vor, Leitung kontrolliert und schliesst ab; abgeschlossene Rechnungen sind unveränderbar."""
    db = app[DB]
    lea, peter = pid_of(app, "Lea"), pid_of(app, "Peter")
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    # UVG: 5-Minuten-Einheiten à 10.03 TP, Taxpunktwert CHF 1.00, externer Faktor 0.94 ab 1.10.2026
    r = await client.post("/empfang/abrechnung", data={"_csrf": csrf, "do": "create", "patient_id": str(lea)}, allow_redirects=False)
    iid = int(r.headers["Location"].rsplit("/", 1)[1])
    lines = db.query("SELECT * FROM invoice_lines WHERE invoice_id = ?", (iid,))
    assert lines and all(ln["code"] == "25.110" and ln["tp_value"] == 1.0 for ln in lines)
    ef = lambda day: 0.94 if day >= "2026-10-01" else 1.0  # noqa: E731
    assert all(ln["external_factor"] == ef(ln["service_date"]) for ln in lines)
    assert all(abs(ln["amount_chf"] - round(ln["quantity"] * 10.03 * ef(ln["service_date"]), 2)) < 0.001 for ln in lines)
    # Empfang darf nicht abschliessen
    await client.post(f"/empfang/rechnungen/{iid}", data={"_csrf": csrf, "do": "finalize"})
    assert db.value("SELECT status FROM invoices WHERE id = ?", (iid,)) == "draft"
    db.execute("DELETE FROM tax_point_values WHERE tariff = 'kvg312'")   # Demo hat einen Demo-Wert – hier: Praxis ohne Wert
    r = await client.post("/empfang/abrechnung", data={"_csrf": csrf, "do": "create", "patient_id": str(peter)}, allow_redirects=False)
    iid2 = int(r.headers["Location"].rsplit("/", 1)[1])
    # Leitung: KVG ohne Taxpunktwert → Abschluss blockiert; Taxpunktwert setzen → Entwurf neu berechnet
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    await client.post(f"/leitung/rechnungen/{iid2}", data={"_csrf": csrf, "do": "finalize"})
    assert db.value("SELECT status FROM invoices WHERE id = ?", (iid2,)) == "draft"
    await client.post("/leitung/einstellungen/tarife", data={"_csrf": csrf, "tariff": "kvg312", "value": "1.00", "valid_from": "2026-01-01"})
    assert db.value("SELECT MIN(tp_value) FROM invoice_lines WHERE invoice_id = ?", (iid2,)) == 1.0
    await client.post(f"/leitung/rechnungen/{iid2}", data={"_csrf": csrf, "do": "finalize"})
    assert db.value("SELECT status FROM invoices WHERE id = ?", (iid2,)) == "final"
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE invoice_lines SET amount_chf = 0 WHERE invoice_id = ?", (iid2,))
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM invoices WHERE id = ?", (iid2,))
    # Termine erscheinen erst nach Storno wieder als offen
    r = await client.get("/leitung/abrechnung?ansicht=bereit")
    assert f'name="patient_id" value="{peter}"' not in await r.text()
    await client.post(f"/leitung/rechnungen/{iid2}", data={"_csrf": csrf, "do": "cancel", "reason": "Test"})
    assert db.value("SELECT status FROM invoices WHERE id = ?", (iid2,)) == "cancelled"
    r = await client.get("/leitung/abrechnung?ansicht=bereit")
    assert f'name="patient_id" value="{peter}"' in await r.text()
