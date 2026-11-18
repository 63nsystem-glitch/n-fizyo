"""Datenübernahme aus der bisherigen Software (Excel/CSV): Zuordnung, Probelauf, Duplikate, Fehler, Übernahme."""
from __future__ import annotations

import io
import re
from pathlib import Path

import aiohttp
from conftest import csrf_of, login

from nfizyo.core.db import utcnow
from nfizyo.core.keys import DB
from nfizyo.modules.dataimport import engine, reader

PATIENTS_CSV = (
    "Patientennr;Name;Vorname;Geburtsdatum;Geschlecht;Strasse;PLZ;Ort;Krankenkasse;AHV-Nr;Telefon\n"
    "A-100;Rüegg;Käthi;03.04.1955;W;Seestrasse 5;2502;Biel;CSS;756.9217.0769.85;079 111 22 33\n"
    "A-101;Muster;Lea;05.03.1992;w;;2500;Biel;Suva;;\n"          # gibt es schon (Demo)
    "A-102;;Ohne;01.01.1980;m;;;;;;\n"                              # Name fehlt
    "A-103;Dupont;Jean;1971-12-24;Herr;Rue Haute 2;2502;Bienne;Unbekannte Kasse;756.1234.5678.90;\n"  # AHV falsch, Kasse unbekannt
    "A-100;Rüegg;Käthi;03.04.1955;W;;;;;;\n"                        # zweimal in der Datei
).encode("cp1252")


def test_guess_mapping_german_and_french():
    m = engine.guess_mapping("patients", ["Patientennr", "Name", "Vorname", "Geburtsdatum", "PLZ", "Ort", "Krankenkasse", "AHV-Nr"])
    assert m["last_name"] == 1 and m["first_name"] == 2 and m["birthdate"] == 3 and m["insurer"] == 6 and m["ahv_number"] == 7
    m = engine.guess_mapping("patients", ["Nom", "Prénom", "Date de naissance", "NPA", "Localité"])
    assert m == {"last_name": 0, "first_name": 1, "birthdate": 2, "zip": 3, "city": 4}


def test_parsers():
    assert engine.parse_date("3.4.55") == "1955-04-03" and engine.parse_date("2026-11-03") == "2026-11-03"
    assert engine.parse_date("31.02.2020") is None and engine.parse_date("45000") == "2023-03-15"
    assert engine.parse_time("8.30") == "08:30" and engine.parse_time("0930") == "09:30" and engine.parse_time("25:00") is None
    assert engine.parse_sex("Frau") == "f" and engine.parse_sex("Herr") == "m" and engine.parse_sex("?") is None
    headers, rows = reader.read_csv(PATIENTS_CSV)
    assert headers[1] == "Name" and rows[0][1] == "Rüegg"


async def _upload(client, kind: str, data: bytes, filename: str) -> str:
    html = await (await client.get("/leitung/import")).text()
    form = aiohttp.FormData()
    form.add_field("_csrf", csrf_of(html))
    form.add_field("kind", kind)
    form.add_field("file", data, filename=filename)
    r = await client.post("/leitung/import", data=form, allow_redirects=False)
    assert r.status == 302, await r.text()
    return r.headers["Location"]


async def test_patients_import_flow(client, app):
    db = app[DB]
    await login(client, "leitung@demo.n-fizyo.ch")
    url = await _upload(client, "patients", PATIENTS_CSV, "alt.csv")
    job = db.one("SELECT * FROM import_jobs ORDER BY id DESC LIMIT 1")
    assert Path(job["path"]).exists() and job["rows"] == 5
    html = await (await client.get(url)).text()
    assert "Käthi" in html  # Vorschau der Datei
    # Probelauf mit der vorgeschlagenen Zuordnung
    assert re.search(r'<option value="1" selected>Name</option>', html)  # Vorschlag: Spalte «Name» → Name
    headers, _ = reader.read_csv(PATIENTS_CSV)
    selects = {f"f_{k}": str(v) for k, v in engine.guess_mapping("patients", headers).items()}
    await client.post(url, data={"_csrf": csrf_of(html), "do": "map", **selects}, allow_redirects=False)
    job = db.one("SELECT * FROM import_jobs WHERE id = ?", (job["id"],))
    import json

    preview = json.loads(job["preview"])
    assert (preview["new"], preview["duplicate"], preview["error"]) == (2, 2, 1)
    assert preview["notes"]["bad_ahv"] == 1 and preview["notes"]["unknown_insurer"] == 1 and preview["notes"]["dup_in_file"] == 1
    before = db.value("SELECT COUNT(*) FROM patients WHERE practice_id = 1")
    html = await (await client.get(url)).text()
    await client.post(url, data={"_csrf": csrf_of(html), "do": "run"})
    assert db.value("SELECT COUNT(*) FROM patients WHERE practice_id = 1") == before + 2
    k = db.one("SELECT * FROM patients WHERE last_name = 'Rüegg'")
    assert k["first_name"] == "Käthi" and k["birthdate"] == "1955-04-03" and k["sex"] == "f" and k["number"] == "A-100"
    assert k["ahv_number"] == "7569217076985" and k["insurer_id"] == db.value("SELECT id FROM insurers WHERE name = 'CSS'")
    j = db.one("SELECT * FROM patients WHERE last_name = 'Dupont'")
    assert j["ahv_number"] is None and j["insurer_id"] is None and j["birthdate"] == "1971-12-24"
    job = db.one("SELECT * FROM import_jobs WHERE id = ?", (job["id"],))
    from nfizyo.core.keys import SETTINGS

    assert job["status"] == "done" and job["path"] is None
    assert not list((app[SETTINGS].data_dir / "imports" / "1").iterdir())  # Datei gelöscht
    # Hinweisliste ohne Personendaten
    text = (await (await client.get(url + "/hinweise.csv")).read()).decode("utf-8-sig")
    assert "Zeile" in text and "Rüegg" not in text and "Dupont" not in text and "Name fehlt" in text
    # zweite Übernahme der gleichen Datei → alles doppelt
    url2 = await _upload(client, "patients", PATIENTS_CSV, "alt.csv")
    html = await (await client.get(url2)).text()
    await client.post(url2, data={"_csrf": csrf_of(html), "do": "map", **selects})
    p2 = json.loads(db.value("SELECT preview FROM import_jobs ORDER BY id DESC LIMIT 1"))
    assert p2["new"] == 0


async def test_insurers_xlsx_and_appointments(client, app):
    from openpyxl import Workbook

    db = app[DB]
    wb = Workbook()
    ws = wb.active
    ws.append(["Versicherer", "Gesetz", "GLN", "Strasse", "PLZ", "Ort"])
    ws.append(["Neue Kasse AG", "KVG", "2099990000999", "Weg 1", "8000", "Zürich"])
    ws.append(["Suva", "UVG", "", "", "", ""])   # gibt es schon
    buf = io.BytesIO()
    wb.save(buf)
    await login(client, "leitung@demo.n-fizyo.ch")
    url = await _upload(client, "insurers", buf.getvalue(), "kassen.xlsx")
    html = await (await client.get(url)).text()
    await client.post(url, data={"_csrf": csrf_of(html), "do": "map", "f_name": "0", "f_law": "1", "f_gln": "2", "f_street": "3",
                                 "f_zip": "4", "f_city": "5"})
    html = await (await client.get(url)).text()
    await client.post(url, data={"_csrf": csrf_of(html), "do": "run"})
    ins = db.one("SELECT * FROM insurers WHERE name = 'Neue Kasse AG'")
    assert ins["law"] == "KVG" and ins["tiers"] == "garant" and ins["gln"] == "2099990000999" and ins["city"] == "Zürich"
    assert db.value("SELECT COUNT(*) FROM insurers WHERE name = 'Suva'") == 1

    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from nfizyo.core.timeutil import today

    day1, day2 = today() + timedelta(days=30), today() + timedelta(days=31)   # immer in der Zukunft → «geplant»
    d1, d2 = day1.strftime("%d.%m.%Y"), day2.strftime("%d.%m.%Y")
    csv_data = ("Name;Vorname;Geb.;Datum;Zeit;Dauer;Therapeut;Status\n"
                f"Muster;Lea;05.03.1992;{d1};09:30;30;TD;\n"
                f"Muster;Lea;05.03.1992;{d1};09:30;30;TD;\n"
                f"Niemand;Nobody;01.01.2000;{d1};10:00;30;TD;\n"
                f"Muster;Lea;05.03.1992;{d2};10:00;30;XY;\n").encode()
    url = await _upload(client, "appointments", csv_data, "termine.csv")
    html = await (await client.get(url)).text()
    await client.post(url, data={"_csrf": csrf_of(html), "do": "map", "f_last_name": "0", "f_first_name": "1", "f_birthdate": "2",
                                 "f_date": "3", "f_start": "4", "f_minutes": "5", "f_therapist": "6", "f_status": "7"})
    import json

    preview = json.loads(db.value("SELECT preview FROM import_jobs ORDER BY id DESC LIMIT 1"))
    assert (preview["new"], preview["duplicate"], preview["error"]) == (1, 1, 2)
    assert preview["notes"]["unknown_patient"] == 1 and preview["notes"]["unknown_therapist"] == 1
    html = await (await client.get(url)).text()
    await client.post(url, data={"_csrf": csrf_of(html), "do": "run"})
    lea = db.value("SELECT id FROM patients WHERE first_name = 'Lea'")
    appt = db.one("SELECT * FROM appointments WHERE patient_id = ? ORDER BY id DESC LIMIT 1", (lea,))
    utc = datetime(day1.year, day1.month, day1.day, 9, 30, tzinfo=ZoneInfo("Europe/Zurich")).astimezone(timezone.utc)
    assert appt["status"] == "booked" and appt["starts_at"].startswith(utc.strftime("%Y-%m-%dT%H:%M"))  # 09:30 Zürich in UTC


async def test_only_leitung_and_own_practice(client, app, practice_b):
    db = app[DB]
    jid = db.execute("INSERT INTO import_jobs (practice_id, kind, filename, rows, created_at) VALUES (?,?,?,?,?)",
                     (practice_b["id"], "patients", "b.csv", 1, utcnow()))
    await login(client, "leitung@demo.n-fizyo.ch")
    assert (await client.get(f"/leitung/import/{jid}", allow_redirects=False)).status == 404
    assert (await client.get(f"/leitung/import/{jid}/hinweise.csv", allow_redirects=False)).status == 404
    assert "b.csv" not in await (await client.get("/leitung/import")).text()
    await login(client, "empfang@demo.n-fizyo.ch")
    assert (await client.get("/leitung/import", allow_redirects=False)).status in (403, 404)
