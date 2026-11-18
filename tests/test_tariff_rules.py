"""KVG-Tarifstruktur 312 vollständig (mit Quelle), Frankenpositionen, Zuschlag erste Behandlung, Kombinationsregeln,
Tarifwechsel nach Behandlungstag und Video-Sitzungen in der Abrechnung."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from conftest import login, pid_of

from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.core.timeutil import TZ
from nfizyo.knowledge import tariffs
from nfizyo.modules.agenda import repo as agenda
from nfizyo.modules.billing import repo as billing


def test_kvg_312_catalog_matches_the_tariff_structure():
    kvg = {p.code: p for p in tariffs.positions("kvg312")}
    expected = {"7301": 48, "7311": 77, "7313": 77, "7330": 25, "7340": 22, "7350": 24, "7351": 30, "7352": 19, "7353": 67,
                "7354": 34, "7362": 50, "7363": 90}
    assert {c: kvg[c].tp for c in expected} == expected and "7361" in kvg
    assert kvg["7362"].fixed_chf and kvg["7361"].unit == "chf_var" and not kvg["7301"].fixed_chf
    assert "7340" not in {c for allowed in tariffs.KVG_ALLOWED.values() for c in allowed}      # MTT ohne Zuschläge
    assert tariffs.tariff_for_law("KVG", "2026-11-02").key == "kvg312"
    assert tariffs.tariff_for_law("UVG", "2026-11-02").remote_code == "25.310"


def _patient(db, practice_id, user, law="KVG", first="Kora"):
    from nfizyo.core import access
    from nfizyo.core import patients as core_patients

    with practice_scope(practice_id, user):
        pid = core_patients.create(db, {"first_name": first, "last_name": "Tarif", "birthdate": "1980-01-01", "sex": "f", "lang": "de",
                                        "law": law, "therapist_id": user})
        access.add_to_team(db, pid, user, "main")
    return pid


def _done(db, practice_id, user, pid, days_ago: int, type_key: str | None = None, minutes: int = 30) -> int:
    with practice_scope(practice_id, user):
        tt = db.value("SELECT id FROM treatment_types WHERE practice_id = ? AND tariff_code = ? LIMIT 1", (practice_id, type_key)) \
            if type_key else None
        start = (datetime.now(TZ) - timedelta(days=days_ago)).replace(hour=9, minute=0, second=0, microsecond=0)
        return agenda.create(db, therapist_id=user, start=start, minutes=minutes, patient_id=pid, treatment_type_id=tt, status="done")


def test_first_treatment_supplement_and_franc_positions(app):
    db = app[DB]
    user = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    pid = _patient(db, 1, user)
    _done(db, 1, user, pid, 3, "7301")
    _done(db, 1, user, pid, 1, "7301")
    with practice_scope(1, user):
        billing.set_tp_value(db, "kvg312", 1.0, "2018-01-01")
        from nfizyo.core import patients as core_patients

        iid = billing.create_from_appointments(db, core_patients.get(db, pid), "de")
        codes = [ln["code"] for ln in billing.lines(db, iid)]
        assert codes.count("7350") == 1 and codes.count("7301") == 2                    # 7350 nur zur ersten Sitzung
        inv = billing.get(db, iid)
        day = billing.lines(db, iid)[0]["service_date"]
        billing.add_line(db, inv, code="7362", quantity=1, service_date=day, lang="de")
        billing.add_line(db, inv, code="7361", quantity=1, service_date=day, lang="de", chf=12.4, detail="Tape, 5 m")
        billing.add_line(db, inv, code="7361", quantity=1, service_date=day, lang="de")      # ohne Betrag: nichts
        lns = {ln["code"]: ln for ln in billing.lines(db, iid)}
        assert lns["7362"]["amount_chf"] == 50.0 and lns["7362"]["tp_value"] == 1.0
        assert lns["7361"]["amount_chf"] == 12.4 and "Tape, 5 m" in lns["7361"]["label"]
        billing.set_tp_value(db, "kvg312", 1.10, "2018-01-01")
        billing.reprice_drafts(db, "kvg312")
        lns = {ln["code"]: ln for ln in billing.lines(db, iid)}
        assert lns["7362"]["amount_chf"] == 50.0 and lns["7301"]["amount_chf"] == round(48 * 1.10, 2)
        assert not [p for p in billing.problems(db, inv, billing.lines(db, iid)) if p.startswith("kvg_")]
        # zweite Rechnung später: kein neuer 7350
        _done(db, 1, user, pid, 0, "7301")
        iid2 = billing.create_from_appointments(db, core_patients.get(db, pid), "de")
        assert "7350" not in [ln["code"] for ln in billing.lines(db, iid2)]


def test_combination_rules_block_or_hint(app):
    db = app[DB]
    user = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    pid = _patient(db, 1, user, first="Mira")
    _done(db, 1, user, pid, 2, "7340")
    with practice_scope(1, user):
        billing.set_tp_value(db, "kvg312", 1.0, "2018-01-01")
        from nfizyo.core import patients as core_patients

        iid = billing.create_from_appointments(db, core_patients.get(db, pid), "de")
        assert [ln["code"] for ln in billing.lines(db, iid)] == ["7340"]                 # kein 7350 zu MTT
        inv = billing.get(db, iid)
        day = billing.lines(db, iid)[0]["service_date"]
        billing.add_line(db, inv, code="7350", quantity=1, service_date=day, lang="de")
        assert "kvg_combination" in billing.problems(db, inv, billing.lines(db, iid))
        billing.remove_line(db, inv, next(ln["id"] for ln in billing.lines(db, iid) if ln["code"] == "7350"))
        billing.add_line(db, inv, code="7301", quantity=1, service_date=day, lang="de")
        assert "kvg_two_sessions" in billing.warnings(db, inv, billing.lines(db, iid))
        assert "kvg_two_sessions" not in billing.problems(db, inv, billing.lines(db, iid))
        billing.add_line(db, inv, code="7351", quantity=1, service_date=day, lang="de")
        assert {"kvg_7351_age", "kvg_combination"} <= set(billing.problems(db, inv, billing.lines(db, iid)))
        billing.add_line(db, inv, code="7362", quantity=1, service_date=day, lang="de")
        billing.add_line(db, inv, code="7363", quantity=1, service_date=day, lang="de")
        assert "kvg_probe_year" in billing.problems(db, inv, billing.lines(db, iid))


def test_new_tariff_from_a_date_splits_invoices(app, monkeypatch):
    """Neue KVG-Struktur: ab Stichtag eigener Tarif – Rechnungen werden an der Grenze getrennt."""
    db = app[DB]
    user = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    pid = _patient(db, 1, user, first="Nova")
    _done(db, 1, user, pid, 10, "7301")
    _done(db, 1, user, pid, 2, "7301", minutes=40)
    cut = (datetime.now(TZ) - timedelta(days=5)).date().isoformat()
    old = tariffs.TARIFFS["kvg312"]
    new = tariffs.Tariff("kvgneu", ("KVG",), "KVG neu (Test)", "LAMal nouveau (test)", cut, None, "Test", code="312",
                         default_code="99.010")
    monkeypatch.setitem(tariffs.TARIFFS, "kvg312", replace(old, valid_until=(datetime.fromisoformat(cut) - timedelta(days=1)).date().isoformat()))
    monkeypatch.setitem(tariffs.TARIFFS, "kvgneu", new)
    pos = tariffs.Position("kvgneu", "99.010", "Behandlung, pro 5 Min.", "Traitement, par 5 min", 9.0, "5min")
    monkeypatch.setitem(tariffs.BY_CODE, ("kvgneu", "99.010"), pos)
    with practice_scope(1, user):
        billing.set_tp_value(db, "kvg312", 1.0, "2018-01-01")
        billing.set_tp_value(db, "kvgneu", 1.0, "2000-01-01")
        from nfizyo.core import patients as core_patients

        first = billing.create_from_appointments(db, core_patients.get(db, pid), "de")
        invs = db.query("SELECT * FROM invoices WHERE patient_id = ? ORDER BY id", (pid,))
        assert [i["tariff"] for i in invs] == ["kvg312", "kvgneu"] and invs[0]["id"] == first
        new_lines = billing.lines(db, invs[1]["id"])
        assert [(ln["code"], ln["quantity"]) for ln in new_lines] == [("99.010", 8.0)]


async def test_video_session_billing(client, app):
    db = app[DB]
    from nfizyo.modules.video import repo as video

    user = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    uvg = _patient(db, 1, user, law="UVG", first="Ugo")
    kvg = _patient(db, 1, user, law="KVG", first="Kai")
    a_uvg = _done(db, 1, user, uvg, 1, minutes=30)
    a_kvg = _done(db, 1, user, kvg, 1, "7301")
    with practice_scope(1, user):
        for aid, pid in ((a_uvg, uvg), (a_kvg, kvg)):
            rid = video.create_room(db, aid, pid)
            video.mark_joined(db, rid, "therapist")
        billing.set_tp_value(db, "kvg312", 1.0, "2018-01-01")
        from nfizyo.core import patients as core_patients

        iid = billing.create_from_appointments(db, core_patients.get(db, uvg), "de")
        assert [(ln["code"], ln["quantity"]) for ln in billing.lines(db, iid)] == [("25.310", 6.0)]
        iid = billing.create_from_appointments(db, core_patients.get(db, kvg), "de")
        inv = billing.get(db, iid)
        assert "kvg_video" in billing.warnings(db, inv, billing.lines(db, iid))
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    page = await (await client.get(f"/leitung/rechnungen/{iid}")).text()
    assert "Video-Sitzung: Die KVG-Tarifstruktur hat keine eigene Position" in page
    assert csrf and pid_of(app, "Kai")


def test_kvg_xml_with_supplements_is_schema_valid(app):
    """KVG-Rechnung mit 7350 (automatisch) und Frankenpositionen 7361/7362 – gültig nach dem offiziellen XML-Schema 5.0."""
    from nfizyo.modules.billing import xml500

    db = app[DB]
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    with practice_scope(1, owner):
        billing.set_tp_value(db, "kvg312", 1.05, "2018-01-01")
        db.execute("UPDATE patients SET ahv_number = '7561234567897' WHERE id = ?", (pid_of(app, "Peter"),))   # fiktiv, Prüfziffer gültig
        patient = db.one("SELECT * FROM patients WHERE id = ?", (pid_of(app, "Peter"),))
        iid = billing.create_from_appointments(db, patient, "de")
        inv = billing.get(db, iid)
        assert "7350" in [ln["code"] for ln in billing.lines(db, iid)]
        day = billing.lines(db, iid)[0]["service_date"]
        billing.add_line(db, inv, code="7362", quantity=1, service_date=day, lang="de")
        billing.add_line(db, inv, code="7361", quantity=1, service_date=day, lang="de", chf=8.9, detail="Tape, 5 m")
        lns = billing.lines(db, iid)
        assert not billing.problems(db, inv, lns), billing.problems(db, inv, lns)
        billing.finalize(db, billing.get(db, iid))
        data = billing.xml_data(db, billing.get(db, iid))
        assert xml500.missing(data) == []
        xml, _ = xml500.build(data, modus="test")
        xml500.validate(xml)
    text = xml.decode()
    assert 'code="7362"' in text and 'code="7361"' in text and 'tariff_type="312"' in text
