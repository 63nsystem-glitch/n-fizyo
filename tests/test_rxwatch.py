"""Verordnungen im Blick: Regeln (9 Sitzungen, 5 Wochen, 36 Sitzungen, unvollständig) und Bildschirme."""
from __future__ import annotations

from datetime import date, timedelta

from conftest import login, pid_of

from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.modules.rxwatch import rules

D0 = date(2026, 11, 2)


def _inp(**kw):
    base = {"prescription": {"id": 1, "sessions": 9, "issued_on": (D0 - timedelta(days=20)).isoformat(), "diagnosis": "Knie",
                             "referrer_id": 1}, "law": "KVG", "used": 3, "future": [], "chain_used": 3, "today": D0}
    base.update(kw)
    return rules.RxInput(**base)


def _codes(result):
    return {f.code: f for f in result["flags"]}


def test_booked_beyond_prescription_is_flagged_with_date():
    days = [D0 + timedelta(days=n) for n in (1, 3, 5, 9)]
    r = rules.evaluate(_inp(used=7, future=days, chain_used=7))
    f = _codes(r)["uncovered"]
    assert f.params == {"date": days[2], "booked": 4, "left": 2} and f.level == "act" and f.request_kind == "renewal"
    assert r["level"] == "act" and r["covered_until"] == days[1]
    later = rules.evaluate(_inp(used=7, future=[D0 + timedelta(days=n) for n in (1, 3, 12)], chain_used=7))
    assert _codes(later)["uncovered"].level == "soon"


def test_few_sessions_left_reminds_to_renew():
    r = rules.evaluate(_inp(used=7, future=[D0 + timedelta(days=2)], chain_used=7))
    assert set(_codes(r)) == {"renew"} and r["level"] == "soon"
    assert rules.evaluate(_inp(used=4, future=[D0 + timedelta(days=2)]))["level"] == "ok"


def test_request_replaces_warning_and_nudges_after_a_week():
    days = [D0 + timedelta(days=n) for n in (1, 3, 5)]
    fresh = rules.evaluate(_inp(used=8, future=days, chain_used=8,
                                requests=[{"kind": "renewal", "date": D0 - timedelta(days=2), "via": "phone"}]))
    assert set(_codes(fresh)) == {"renewal_requested"} and fresh["level"] == "info"
    old = rules.evaluate(_inp(used=8, future=days, chain_used=8,
                              requests=[{"kind": "renewal", "date": D0 - timedelta(days=9), "via": "fax"}]))
    f = _codes(old)["renewal_requested"]
    assert f.params["nudge"] and f.params["days"] == 9 and f.level == "act"


def test_five_week_start_deadline():
    issued = (D0 - timedelta(days=40)).isoformat()
    missed = rules.evaluate(_inp(prescription={"id": 1, "sessions": 9, "issued_on": issued, "diagnosis": "x", "referrer_id": 1},
                                 used=0, chain_used=0))
    assert "start_missed" in _codes(missed)
    issued = (D0 - timedelta(days=31)).isoformat()
    late = rules.evaluate(_inp(prescription={"id": 1, "sessions": 9, "issued_on": issued, "diagnosis": "x", "referrer_id": 1},
                               used=0, chain_used=0, future=[D0 + timedelta(days=7)]))
    assert _codes(late)["start_late"].params["deadline"] == D0 + timedelta(days=4)
    issued = (D0 - timedelta(days=30)).isoformat()
    soon = rules.evaluate(_inp(prescription={"id": 1, "sessions": 9, "issued_on": issued, "diagnosis": "x", "referrer_id": 1},
                               used=0, chain_used=0))
    assert _codes(soon)["start_soon"].params["days"] == 5
    # Unfallversicherung: keine 5-Wochen-Regel, keine 36er-Grenze
    uvg = rules.evaluate(_inp(prescription={"id": 1, "sessions": 9, "issued_on": issued, "diagnosis": "x", "referrer_id": 1},
                              law="UVG", used=0, chain_used=40, future=[D0 + timedelta(days=9)]))
    assert not ({"start_soon", "start_late", "kogu"} & set(_codes(uvg)))


def test_incomplete_prescription_only_doctor_can_fix():
    r = rules.evaluate(_inp(prescription={"id": 1, "sessions": 9, "issued_on": None, "diagnosis": "", "referrer_id": None},
                            future=[D0 + timedelta(days=1)]))
    f = _codes(r)["incomplete"]
    assert f.params["fields"] == ["diagnosis", "referrer", "issued_on"] and f.level == "act" and f.request_kind == "correction"


def test_cost_approval_from_session_37():
    days = [D0 + timedelta(days=n) for n in (2, 4, 6, 20)]
    r = rules.evaluate(_inp(used=7, chain_used=34, future=days))
    f = _codes(r)["kogu"]
    assert f.params["date"] == days[2] and f.level == "act"
    assert "kogu_soon" in _codes(rules.evaluate(_inp(used=5, chain_used=31, future=[D0 + timedelta(days=2)])))
    # gültige Kostengutsprache in der Abrechnung → keine Warnung
    assert not ({"kogu", "kogu_soon"} & set(_codes(rules.evaluate(_inp(used=7, chain_used=34, future=days, kogu_ok=True)))))


# --------------------------------------------------------------------------- Bildschirme (Demo-Praxis)
async def test_therapist_and_reception_see_what_to_do(client, app):
    await login(client, "therapie@demo.n-fizyo.ch")
    text = await (await client.get("/therapie/verordnungen")).text()
    assert "Peter" in text and "ohne gültige Verordnung" in text
    assert "Jonas" in text and "5-Wochen-Frist" in text
    assert "Sandra" not in text  # nicht im eigenen Team
    assert "Gesuch an Ärztin / Arzt erstellen" in text
    heute = await (await client.get("/therapie/heute")).text()
    assert "Verordnungen prüfen" in heute
    akte = await (await client.get(f"/therapie/patienten/{pid_of(app, 'Peter')}")).text()
    assert "ohne gültige Verordnung" in akte
    await login(client, "empfang@demo.n-fizyo.ch")
    text = await (await client.get("/empfang/verordnungen")).text()
    assert "Peter" in text and "Jonas" in text
    assert "St. n. Knie-TEP" not in text  # Empfang: keine Diagnose
    assert "Gesuch an Ärztin / Arzt erstellen" not in text


async def test_mark_requested_and_close(client, app):
    db = app[DB]
    peter = pid_of(app, "Peter")
    with practice_scope(1):
        prid = db.value("SELECT MAX(id) FROM prescriptions WHERE patient_id = ?", (peter,))
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.post(f"/empfang/verordnungen/{prid}/anfrage", data={"_csrf": csrf, "kind": "renewal", "via": "fax",
                                                                          "back": "/empfang/verordnungen"}, allow_redirects=False)
    assert r.status == 302
    qid = db.value("SELECT id FROM rx_requests WHERE prescription_id = ? AND status = 'open'", (prid,))
    assert qid
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    akte = await (await client.get(f"/therapie/patienten/{peter}")).text()
    assert "Folgeverordnung angefragt am" in akte and "Fax" in akte
    r = await client.post(f"/therapie/verordnungen/anfrage/{qid}", data={"_csrf": csrf, "status": "done"}, allow_redirects=False)
    assert r.status == 302
    assert db.value("SELECT status FROM rx_requests WHERE id = ?", (qid,)) == "done"
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE entity = 'rx_request'") == 2


async def test_therapist_cannot_touch_foreign_patients(client, app):
    db = app[DB]
    sandra = pid_of(app, "Sandra")
    with practice_scope(1):
        prid = db.value("SELECT MAX(id) FROM prescriptions WHERE patient_id = ?", (sandra,))
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post(f"/therapie/verordnungen/{prid}/anfrage", data={"_csrf": csrf, "kind": "renewal", "via": "phone"},
                          allow_redirects=False)
    assert r.status == 404
    assert db.value("SELECT COUNT(*) FROM rx_requests") == 0


def test_boundaries_day_35_and_session_36():
    """Grenzen genau: Beginn am 35. Tag ist noch rechtzeitig, die 36. Sitzung braucht noch keine Kostengutsprache."""
    issued = (D0 - timedelta(days=35)).isoformat()
    rx = {"id": 1, "sessions": 9, "issued_on": issued, "diagnosis": "x", "referrer_id": 1}
    on_time = rules.evaluate(_inp(prescription=rx, used=0, chain_used=0, future=[D0]))
    assert not ({"start_missed", "start_late"} & set(_codes(on_time)))
    assert "start_missed" in _codes(rules.evaluate(_inp(prescription=rx, used=0, chain_used=0, today=D0 + timedelta(days=1))))
    days = [D0 + timedelta(days=n) for n in (1, 2)]
    assert "kogu" not in _codes(rules.evaluate(_inp(used=7, chain_used=34, future=days)))           # 34 + 2 = 36
    assert "kogu" in _codes(rules.evaluate(_inp(used=7, chain_used=35, future=days)))               # 35 + 2 = 37
    # Ohne Angabe gilt das Zürcher Datum (nie das Datum des Servers)
    from nfizyo.core.timeutil import today
    assert rules.RxInput(prescription=rx, law="KVG", used=0, future=[]).today == today()
