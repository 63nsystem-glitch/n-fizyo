"""Gemeinsame Test-Hilfen: Demo-App in einem leeren Temp-Ordner, Login, zweite Praxis für Trennungstests."""
from __future__ import annotations

import re

import pytest

from nfizyo.app import create_app
from nfizyo.core.auth import create_user
from nfizyo.core.config import load_settings
from nfizyo.core.db import utcnow
from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope

PASSWORD = "demo-praxis"


@pytest.fixture(autouse=True)
def _fast_password_hashing(monkeypatch):
    """Tests: scrypt mit N = 2**14 statt 2**17 (sonst ~0,4 s pro Benutzer und Anmeldung). Die echten Werte (2**17) und das
    Erneuern älterer Hashes prüft tests/test_security_090.py."""
    from nfizyo.core import security

    monkeypatch.setattr(security, "SCRYPT_LOG_N", 14)


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


@pytest.fixture
def app(tmp_path):
    settings = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True)
    return create_app(settings, start_workers=False)


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


async def login(client, email: str, password: str = PASSWORD) -> str:
    """Meldet an und gibt das CSRF-Token der Sitzung zurück."""
    client.session.cookie_jar.clear()
    r = await client.get("/login")
    r = await client.post("/login", data={"_csrf": csrf_of(await r.text()), "email": email, "password": password}, allow_redirects=False)
    assert r.status == 302, f"Login fehlgeschlagen für {email}"
    r = await client.get(r.headers["Location"])
    return csrf_of(await r.text())


def pid_of(app, first_name: str) -> int:
    return app[DB].value("SELECT id FROM patients WHERE first_name = ? AND practice_id = 1", (first_name,))


@pytest.fixture
def practice_b(app):
    """Zweite Praxis mit eigener Leitung (zugleich Therapeutin) und Daten in allen Modulen."""
    from nfizyo.core import access
    from nfizyo.core import patients as core_patients
    from nfizyo.modules.agenda import repo as agenda
    from nfizyo.modules.documentation import repo as notes
    from nfizyo.modules.inbox import repo as inbox
    from nfizyo.modules.messages import repo as messages
    from nfizyo.modules.patientinfo import repo as pinfo
    from nfizyo.modules.reports import repo as reports
    from nfizyo.core.keys import SETTINGS

    db = app[DB]
    pid = db.execute("INSERT INTO practices (name, slug, default_lang, created_at) VALUES (?,?,?,?)", ("Praxis B", "praxis-b", "fr", utcnow()))
    uid = create_user(db, pid, "chef@praxis-b.invalid", "Chef B", "owner,therapist", "passwort-b-123", "fr")
    rec = create_user(db, pid, "accueil@praxis-b.invalid", "Accueil B", "reception", "passwort-b-123", "fr")
    with practice_scope(pid, uid):
        patient = core_patients.create(db, {"first_name": "Zoraida", "last_name": "Geheimnisvoll", "birthdate": "1970-01-01", "sex": "f",
                                            "lang": "fr", "therapist_id": uid})
        access.add_to_team(db, patient, uid, "main")
        note = notes.create(db, patient, {"befund": "Geheimer Befund B"})
        report = reports.create(db, patient, kind="progress", lang="fr", referrer_id=None, prescription_id=None, subject="B",
                                salutation="B", sections={"assessment": "Geheim"})
        inv = db.execute("INSERT INTO invoices (practice_id, patient_id, law, tariff, tiers, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                         (pid, patient, "KVG", "kvg312", "garant", uid, utcnow()))
        from datetime import datetime, timedelta

        from nfizyo.core.timeutil import TZ
        appt = agenda.create(db, therapist_id=uid, start=datetime.now(TZ) + timedelta(hours=2), minutes=30, patient_id=patient)
        doc, _ = inbox.store_file(db, app[SETTINGS].documents_dir, "geheim.txt", b"Geheimer Bericht B", source="upload", patient_id=patient)
        msg = messages.add(db, patient, direction="in", body="Geheime Nachricht B", lang="fr")
        info = pinfo.create_approved(db, patient, "Geheime Info B", "", "fr", "fr")
        # Kasse und Preisliste der Praxis B
        from nfizyo.core.timeutil import today
        from nfizyo.modules.billing import private
        from nfizyo.modules.cashdesk import repo as cash

        item = private.save_item(db, {"kind": "abo", "code": "GEHEIM-ABO", "name_de": "Geheimabo B", "name_fr": "Abo secret B",
                                      "price_chf": 99.0, "vat_rate": 0.0, "abo_sessions": 5, "abo_days": 30})
        sale, _ = cash.sell(db, patient=core_patients.get(db, patient), entries=[(private.item(db, item), 1)], method="cash",
                            lang="fr", day=today().isoformat(), note="Geheimverkauf B")
        sub = db.value("SELECT id FROM subscriptions WHERE sale_id = ?", (sale,))
    return {"id": pid, "user": uid, "reception": rec, "patient": patient, "note": note, "report": report, "invoice": inv,
            "appointment": appt, "document": doc, "message": msg, "info": info, "price_item": item, "sale": sale, "subscription": sub,
            "email": "chef@praxis-b.invalid", "reception_email": "accueil@praxis-b.invalid", "password": "passwort-b-123"}
