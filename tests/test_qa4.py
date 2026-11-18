"""4. Prüfrunde (vor dem Pilot): Bedingungen wie im Echtbetrieb – HIN-Post aus echten Mailprogrammen, SMS-Rückmeldungen,
Geräte lange offline, Arbeiten während eines Updates."""
from __future__ import annotations

import io
import re
import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest

from nfizyo.app import create_app
from nfizyo.core import hin
from nfizyo.core.config import load_settings
from nfizyo.core.keys import DB, HIN

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def _docx(paragraphs):
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def _mail(subject, sender, text=None, html=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "praxis@hin.ch", subject
    if text is not None:
        m.set_content(text)
        if html:
            m.add_alternative(html, subtype="html")
    elif html:
        m.set_content(html, subtype="html")
    return m


class FakeIMAP:
    def __init__(self, store):
        self.store = store

    def select(self, box):
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        if cmd == "search":
            return "OK", [b" ".join(str(u).encode() for u, (raw, seen) in self.store.items() if not seen)]
        if cmd == "fetch":
            return "OK", [(b"1 (BODY[]", self.store[int(args[0])][0]), b")"]
        if cmd == "store":
            raw, _ = self.store[int(args[0])]
            self.store[int(args[0])] = (raw, True)
            return "OK", [b""]

    def logout(self):
        pass


@pytest.fixture
def hin_app(tmp_path):
    s = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True, hin_practice="demo", hin_from="praxis@hin.ch",
                      hin_user="praxis", hin_password="x")
    return create_app(s, start_workers=False)


def _texts(db, since):
    out = {}
    for r in db.query("SELECT filename, mime, path FROM documents WHERE id > ? ORDER BY id", (since,)):
        out[r["filename"]] = open(r["path"], encoding="utf-8").read() if r["mime"] == "text/plain" else r["mime"]
    return out


async def test_hin_mail_from_real_mail_programs_never_disappears(hin_app, monkeypatch):
    """Vorher: reine HTML-Mail (Spital) und Word-Anhang verschwanden still (Mail als gelesen markiert),
    das Signatur-Logo (image001.png) wurde ein eigenes Dokument im Posteingang."""
    from nfizyo.modules.inbox.views import hin_poll_once

    outlook = _mail("Bericht Lea Muster", "Dr. Beispiel <dr.beispiel@hin.ch>", "Im Anhang der Bericht.", "<p>Im Anhang</p><img src='cid:i1'>")
    outlook.get_payload()[1].add_related(PNG, maintype="image", subtype="png", cid="<i1>", filename="image001.png", disposition="inline")
    outlook.add_attachment(PDF, maintype="application", subtype="pdf", filename="Bericht_Muster.pdf")
    html_only = _mail("Austrittsbericht", "spital@hin.ch", html="<html><body><h1>Austrittsbericht</h1><p>Diagnose: VKB-Ruptur rechts."
                      "<br>Teilbelastung 15 kg f&uuml;r 6 Wochen.</p><script>x()</script></body></html>")
    word = _mail("Verordnung", "dr.x@hin.ch", "Siehe Anhang.")
    word.add_attachment(_docx(["Verordnung Physiotherapie", "9 Sitzungen, Knie rechts"]), maintype="application",
                        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document", filename="Verordnung.docx")
    old_doc = _mail("Bericht alt", "dr.y@hin.ch")
    old_doc.add_attachment(b"\xd0\xcf\x11\xe0binary", maintype="application", subtype="msword", filename="Bericht.doc")
    store = {i: (bytes(m), False) for i, m in enumerate((outlook, html_only, word, old_doc), start=1)}
    store[5] = (b"kaputt", False)
    real_parse = hin.parse
    monkeypatch.setattr(hin, "parse", lambda uid, raw: (_ for _ in ()).throw(ValueError("x")) if raw == b"kaputt" else real_parse(uid, raw))
    hin_app[HIN]._imap = lambda: FakeIMAP(store)
    db = hin_app[DB]
    since = db.value("SELECT MAX(id) FROM documents") or 0
    await hin_poll_once(hin_app)
    docs = _texts(db, since)
    assert "image001.png" not in docs and docs["Bericht_Muster.pdf"] == "application/pdf"
    assert "VKB-Ruptur rechts" in docs["hin-2.txt"] and "15 kg für 6 Wochen" in docs["hin-2.txt"] and "x()" not in docs["hin-2.txt"]
    assert "9 Sitzungen, Knie rechts" in docs["Verordnung.txt"]
    assert "Bericht.doc" in docs["hin-4.txt"] and "HIN-Postfach öffnen" in docs["hin-4.txt"]
    assert "nicht gelesen werden" in docs["hin-5.txt"]
    assert all(seen for _, seen in store.values())


async def test_one_bad_hin_message_does_not_block_the_others(hin_app, monkeypatch):
    from nfizyo.modules.inbox import repo
    from nfizyo.modules.inbox.views import hin_poll_once

    store = {1: (bytes(_mail("A", "a@hin.ch", "Text A")), False), 2: (bytes(_mail("B", "b@hin.ch", "Text B")), False)}
    hin_app[HIN]._imap = lambda: FakeIMAP(store)
    real = repo.store_file

    def flaky(db, base, name, data, **kw):
        if b"Text A" in data:
            raise OSError("Platte voll")
        return real(db, base, name, data, **kw)
    monkeypatch.setattr(repo, "store_file", flaky)
    assert await hin_poll_once(hin_app) == 1
    assert store[1][1] is False and store[2][1] is True   # A bleibt ungelesen (nächster Versuch), B ist da


async def test_two_empty_hin_mails_are_two_documents(hin_app):
    """Zwei leere (oder zwei unlesbare) Mails von verschiedenen Absendern: zwei Einträge im Posteingang, jeder mit seinem
    Absender. Vorher war die zweite «schon da» (gleicher Text) und überschrieb den Absender der ersten.
    Ein erneut geschickter gleicher Anhang ändert die Herkunft des ersten Eintrags nicht."""
    from nfizyo.modules.inbox.views import hin_poll_once

    first = _mail("", "a@hin.ch")
    second = _mail("", "b@hin.ch")
    pdf1 = _mail("Bericht", "dr.a@hin.ch")
    pdf1.add_attachment(PDF, maintype="application", subtype="pdf", filename="B.pdf")
    pdf2 = _mail("Nochmals", "dr.z@hin.ch")
    pdf2.add_attachment(PDF, maintype="application", subtype="pdf", filename="B.pdf")
    store = {i: (bytes(m), False) for i, m in enumerate((first, second, pdf1, pdf2), start=1)}
    hin_app[HIN]._imap = lambda: FakeIMAP(store)
    db = hin_app[DB]
    since = db.value("SELECT MAX(id) FROM documents") or 0
    await hin_poll_once(hin_app)
    rows = db.query("SELECT filename, sender FROM documents WHERE id > ? ORDER BY id", (since,))
    notes = [r for r in rows if r["filename"].startswith("hin-")]
    assert len(notes) == 2 and {r["sender"] for r in notes} == {"a@hin.ch", "b@hin.ch"}
    pdf = [r for r in rows if r["filename"] == "B.pdf"]
    assert len(pdf) == 1 and pdf[0]["sender"] == "dr.a@hin.ch"


def test_too_large_attachment_is_named_not_dropped(monkeypatch):
    from nfizyo.modules.inbox.views import _hin_files

    monkeypatch.setattr(hin, "MAX_ATTACHMENT", 10)
    m = _mail("Scan", "c@hin.ch", "Gross")
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="Scan.pdf")
    inc = hin.parse("7", bytes(m))
    files = dict(_hin_files(inc, lambda k, **kw: {"inbox.hin_not_taken": "Nicht übernommen:", "inbox.hin_too_large": "zu gross"}.get(k, k)))
    assert "Scan.pdf" in files["hin-7.txt"].decode() and "zu gross" in files["hin-7.txt"].decode()


async def test_sms_is_never_sent_twice_when_retry_loop_wakes_during_send(tmp_path, monkeypatch):
    """Vorher: Sofortversand wartet auf den Anbieter, die Wiederholungsschleife (alle 60 s) nimmt dieselbe Nachricht
    nochmals → Patient bekommt die Erinnerung doppelt (gemessen: 2 Aufrufe beim Anbieter)."""
    import asyncio

    from nfizyo.core import sms
    from nfizyo.core.keys import SMS

    calls = []

    async def slow(url, payload, headers=None):
        calls.append(payload["Recipients"][0])
        await asyncio.sleep(0.3)
        return 200, {"StatusCode": "1"}

    monkeypatch.setattr(sms, "http_post_json", slow)
    app = create_app(load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=False, sms_engine="live", aspsms_userkey="k",
                                   aspsms_password="p"), start_workers=False, seed_demo=False)
    db, svc = app[DB], app[SMS]
    db.execute("INSERT INTO practices (slug, name, sms_provider, created_at) VALUES ('p', 'Praxis', 'aspsms', '2026-01-01')")
    pid = db.value("SELECT id FROM practices")
    mid = svc.send(pid, "reminder", "079 123 45 67", "Termin morgen 10:00")
    await asyncio.sleep(0.05)
    for m in svc.due_retries():
        await svc.deliver(m)
    await asyncio.sleep(0.5)
    assert calls == ["+41791234567"]
    assert db.one("SELECT status FROM outbox WHERE id = ?", (mid,))["status"] == "sent"


def test_outbox_claim_is_exclusive_and_expires(tmp_path):
    from nfizyo.core.db import Database
    from nfizyo.core.sms import claim

    db = Database(tmp_path / "x.sqlite3")
    db.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, status TEXT, next_try_at TEXT)")
    db.execute("INSERT INTO outbox (status) VALUES ('queued')")
    assert claim(db, 1) is True and claim(db, 1) is False          # zweiter Versuch geht leer aus
    db.execute("UPDATE outbox SET next_try_at = '2000-01-01T00:00:00+00:00'")   # Absturz mitten im Versand: Reservierung abgelaufen
    assert claim(db, 1) is True
    assert db.changes("UPDATE outbox SET status = 'sent' WHERE id = 99") == 0   # changes() zählt Treffer, nicht die letzte ID


async def test_shared_tablet_recording_never_lands_under_another_account(client, app):
    """Geprüft im Browser: A nimmt auf, gibt das Tablet weiter; B sieht «andere Person», nichts wird unter B hochgeladen;
    A öffnet später nur die Agenda → die Aufnahme geht hoch (Urheber A). Nach dem Abmelden ist die Offline-Kopie der
    Diktatseite (Patientennamen) gelöscht."""
    from pathlib import Path

    from aiohttp import FormData
    from conftest import login

    csrf = await login(client, "a.beispiel@demo.n-fizyo.ch")
    fd = FormData()
    fd.add_field("client_uuid", "aaaa-bbbb-0042")
    fd.add_field("user", str(app[DB].value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")))
    fd.add_field("audio", b"\x1aE\xdf\xa3x", filename="a", content_type="audio/webm")
    r = await client.post("/therapie/diktat/upload", data=fd, headers={"X-CSRF-Token": csrf})
    assert r.status == 409 and (await r.json())["error"] == "other_user"
    assert not app[DB].value("SELECT 1 FROM recordings WHERE client_uuid = 'aaaa-bbbb-0042'")
    page = await (await client.get("/therapie/heute")).text()
    me = app[DB].value("SELECT id FROM users WHERE email = 'a.beispiel@demo.n-fizyo.ch'")
    assert f'<meta name="nf-user" content="{me}">' in page
    quick = await (await client.get("/therapie/diktat")).text()
    assert f'data-user="{me}"' in quick
    static = Path(__file__).parents[1] / "nfizyo/web/static/js"
    js, appjs = (static / "dictation.js").read_text(encoding="utf-8"), (static / "app.js").read_text(encoding="utf-8")
    assert "!mine(item)" in js and "fd.append('user', item.user)" in js and "navigator.storage.persist" in js
    assert "caches.delete(k)" in appjs and "/static/js/dictation.js" in appjs


async def test_dropped_upload_leaves_no_half_audio_file(aiohttp_client, app, tmp_path):
    """Handy verliert mitten im Hochladen das Netz: vorher blieb eine halbe Audiodatei (Gesundheitsdaten) für immer liegen."""
    import asyncio
    import os
    import time

    from conftest import login

    from nfizyo.core.keys import SETTINGS
    from nfizyo.modules.dictation.views import purge_partial_uploads

    client = await aiohttp_client(app)
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    cookie = "; ".join(f"{c.key}={c.value}" for c in client.session.cookie_jar)
    boundary = "xyzBOUNDARY"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"client_uuid\"\r\n\r\nabcd-efgh-0001\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"a\"\r\nContent-Type: audio/webm\r\n\r\n").encode()
    body = head + b"\x1aE\xdf\xa3" + b"x" * 600_000          # … und dann bricht die Verbindung ab
    reader, writer = await asyncio.open_connection(client.server.host, client.server.port)
    writer.write((f"POST /therapie/diktat/upload HTTP/1.1\r\nHost: x\r\nCookie: {cookie}\r\nX-CSRF-Token: {csrf}\r\n"
                  f"Content-Type: multipart/form-data; boundary={boundary}\r\nContent-Length: {len(body) + 5_000_000}\r\n\r\n").encode() + body)
    await writer.drain()
    await asyncio.sleep(0.3)
    writer.close()
    await asyncio.sleep(0.5)
    audio = app[SETTINGS].audio_dir
    assert not list(audio.glob("*/upload-*"))
    # Reste nach einem Neustart mitten im Upload räumt das regelmässige Aufräumen weg
    (audio / "1").mkdir(parents=True, exist_ok=True)
    old = audio / "1" / "upload-dead.webm"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 7 * 3600, time.time() - 7 * 3600))
    fresh = audio / "1" / "upload-now.webm"
    fresh.write_bytes(b"x")
    assert purge_partial_uploads(audio) == 1 and not old.exists() and fresh.exists()


def test_import_reads_amounts_dates_and_files_as_other_programs_export_them():
    """Datenübernahme am ersten Pilottag: «1.290,50» wurde zu CHF 1.29 (offener Posten mit falschem Betrag),
    ISO-Zeitstempel und Excel-Zeitbrüche gingen verloren, «sep=;» und Excel-Unicode-Text (UTF-16) ergaben Zeichensalat."""
    from nfizyo.modules.dataimport import reader
    from nfizyo.modules.dataimport.engine import parse_amount, parse_date, parse_time

    assert parse_amount("1.290,50") == 1290.5 and parse_amount("12.345.678,90") == 12345678.9 and parse_amount("1.290") == 1290.0
    assert parse_amount("1'290.50") == parse_amount("1,290.50") == parse_amount("1 290,50") == 1290.5
    assert parse_amount("0.500") == 0.5 and parse_amount("1.29") == 1.29 and parse_amount("CHF 120.–") == 120.0
    assert parse_date("2026-03-01T10:00:00") == parse_date("2026/03/01") == parse_date("01.03.2026") == "2026-03-01"
    assert parse_time("0.416666667") == "10:00" and parse_time("9.30") == "09:30"
    assert reader.read_csv("sep=;\nName;Vorname\nMuster;Lea\n".encode()) == (["Name", "Vorname"], [["Muster", "Lea"]])
    assert reader.read_csv("Name\tVorname\nMüller\tLéa\n".encode("utf-16")) == (["Name", "Vorname"], [["Müller", "Léa"]])
    from nfizyo.modules.dataimport.engine import _names
    assert _names(["Muster, Lea"], {"last_name": 0, "first_name": 0}) == ("Lea", "Muster")        # eine Spalte «Name, Vorname»
    assert _names(["Muster, Lea", ""], {"last_name": 0, "first_name": 1}) == ("Lea", "Muster")
    assert _names(["Muster", "Lea"], {"last_name": 0, "first_name": 1}) == ("Lea", "Muster")


async def test_dictation_does_not_wait_behind_a_morning_scan_batch(app):
    """Vorher: Warteschlange streng der Reihe nach – 20 gescannte Berichte vom Morgen (Texterkennung, je Minuten) kamen vor dem
    Diktat, das die Therapeutin gerade aufgenommen hat."""
    from nfizyo.core.keys import JOBS

    order = []

    async def record(kind):
        async def h(app, practice_id, ref_id):
            order.append(kind)
        return h

    jobs = app[JOBS]
    jobs.register("read_document", await record("scan"), background=True)
    jobs.register("transcribe", await record("diktat"))
    for i in range(3):
        jobs.enqueue(1, "read_document", 900 + i)
    jobs.enqueue(1, "transcribe", 999)
    await jobs.run_all(app)
    assert order == ["diktat", "scan", "scan", "scan"]


async def test_gln_and_zsr_as_written_on_letters_give_a_valid_invoice_xml(client, app):
    """ZSR so eingetippt, wie sie auf dem Brief steht («H 1234.56», klein «h123456»), machte das XML ungültig – MediData-Versand
    blockiert mit «bitte NSystem melden». KVG-Rechnung ohne ZSR wurde nicht als fehlend gemeldet (Kasse vergütet nicht)."""
    from conftest import login

    from nfizyo.core.swissids import clean_gln, clean_zsr
    from nfizyo.core.tenant import practice_scope
    from nfizyo.modules.billing import repo as billing
    from nfizyo.modules.billing import xml500

    assert clean_zsr("H 1234.56") == clean_zsr("h123456") == "H123456" and clean_zsr("1234567") is None
    assert clean_gln("7601 0012 3456 7") == "7601001234567" and clean_gln("7601 0012 3456") is None
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get("/leitung/einstellungen/praxis")).text()
    form = dict(re.findall(r'name="(name|street|house_no|zip|city|phone|email|gln|canton|default_lang)"[^>]*value="([^"]*)"', html))
    form.update({"_csrf": csrf, "zsr": "h 1234.56", "canton": form.get("canton") or "BE", "default_lang": "de"})
    await client.post("/leitung/einstellungen/praxis", data=form)
    assert db.value("SELECT zsr FROM practices WHERE id = 1") == "H123456"
    r = await client.post("/leitung/einstellungen/praxis", data={**form, "zsr": "12345"})
    assert "ein Buchstabe und 6 Ziffern" in await r.text()
    db.execute("UPDATE practices SET zsr = 'h 1234.56' WHERE id = 1")       # alter Eintrag aus der Zeit vor der Prüfung
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    with practice_scope(1, owner):
        billing.set_tp_value(db, "kvg312", 1.0, "2026-01-01")
        pid = db.value("SELECT id FROM patients WHERE practice_id = 1 AND first_name = 'Peter'")
        iid = billing.create_from_appointments(db, core_patient(db, pid), "de")
        billing.finalize(db, billing.get(db, iid))
        db.execute("UPDATE patients SET ahv_number = '7569217076985' WHERE id = ?", (pid,))
        d = billing.xml_data(db, billing.get(db, iid))
        assert d.biller.zsr == "H123456" and xml500.missing(d) == []
        xml500.build(d)                                                      # gültig gegen das Schema
        db.execute("UPDATE practices SET zsr = NULL WHERE id = 1")
        assert "xml.practice_zsr" in xml500.missing(billing.xml_data(db, billing.get(db, iid)))


def core_patient(db, pid):
    from nfizyo.core import patients

    return patients.get(db, pid)


async def test_team_member_with_wrong_gln_is_not_created_halfway(client, app):
    """Vorher (mit der neuen Prüfung wäre es passiert): Konto angelegt, dann Fehler wegen GLN – Startpasswort nie angezeigt."""
    from conftest import login

    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    data = {"_csrf": csrf, "do": "add", "email": "neu@demo.n-fizyo.ch", "name": "Nora Neu", "lang": "de", "role": "therapist",
            "gln": "7601 0012 345"}
    r = await client.post("/leitung/team", data=data)
    assert "13 Ziffern" in await r.text() and not db.value("SELECT 1 FROM users WHERE email = 'neu@demo.n-fizyo.ch'")
    r = await client.post("/leitung/team", data={**data, "gln": "7601 0012 3456 7"})
    assert db.value("SELECT gln FROM users WHERE email = 'neu@demo.n-fizyo.ch'") == "7601001234567"


def test_messy_master_data_is_flagged_never_a_broken_xml(app):
    """Stammdaten-Fuzzing (Leerzeichen, Tabulator, Emoji, 120 Zeichen, «praxis@»): entweder meldet die Rechnung «fehlt»,
    oder das XML ist gültig – nie erst beim Senden «Schema verletzt, bitte NSystem melden» (vorher 26 Fälle)."""
    import test_billing_payment as tb

    from nfizyo.core.tenant import practice_scope
    from nfizyo.modules.billing import repo as billing
    from nfizyo.modules.billing import xml500

    db = app[DB]
    inv = tb._final_invoice(app, "Lea")
    ins = db.value("SELECT insurer_id FROM invoices WHERE id = ?", (inv["id"],))
    values = ["", " ", "\t", "O'Brien-Müller", "x" * 120, "😀 Emoji", "<b>&amp;</b>", "praxis@"]
    targets = [("patients", inv["patient_id"], ("first_name", "last_name", "street", "zip", "city", "email")),
               ("practices", 1, ("name", "street", "zip", "city", "email")), ("insurers", ins, ("name", "street", "zip", "city"))]
    broken = []
    with practice_scope(1):
        for table, rid, cols in targets:
            have = {c["name"] for c in db.query(f"PRAGMA table_info({table})")}
            for col in (c for c in cols if c in have):
                orig = db.value(f"SELECT {col} FROM {table} WHERE id = ?", (rid,))
                for v in values:
                    db.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", (v, rid))
                    d = billing.xml_data(db, billing.get(db, inv["id"]))
                    if not xml500.missing(d):
                        try:
                            xml500.build(d)
                        except xml500.XmlInvalid:
                            broken.append((table, col, v))
                db.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", (orig, rid))
    assert broken == []


def test_qr_payment_part_keeps_long_names_inside_their_column():
    """Lange Praxis- und Doppelnamen liefen im Empfangsschein über den QR-Code und im Zahlteil über den Seitenrand
    (Bank kann den QR-Code schlechter lesen, SIX-Layout verletzt). Jetzt umbrochen, im Empfangsschein nötigenfalls kleiner."""
    import io as _io

    from pypdf import PdfReader
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen.canvas import Canvas

    from nfizyo.core import pdf

    buf = _io.BytesIO()
    c = Canvas(buf, pagesize=A4)
    pdf.draw_qr_bill(c, qr_data="SPC\n0200\n1", account="CH44 3199 9123 0008 8901 2",
                     creditor_lines=["Physiotherapie und Rehabilitationszentrum am Bielersee Dr. Muster-Beispiel & Partnerinnen AG",
                                     "Rue de l’Église Saint-Jean-Baptiste du Haut-Plateau 1", "2500 Biel/Bienne"],
                     reference="00 00000 00000 00000 00000 00011", info="Rechnung 2026-0001",
                     debtor_lines=["Marie-Christine Anne-Sophie von Wattenwyl-de Graffenried-Müller",
                                   "Chemin des Grands-Champs-de-la-Haute-Ville 127b", "2500 Biel/Bienne"], amount="360.00")
    c.save()
    spans = []

    def visit(text, cm, tm, font, size):
        if text.strip():
            x, y = tm[4] + cm[4], tm[5] + cm[5]
            spans.append((x, y, text, size or 8))
    PdfReader(_io.BytesIO(buf.getvalue())).pages[0].extract_text(visitor_text=visit)
    assert len(spans) > 15
    from reportlab.pdfbase.pdfmetrics import stringWidth
    for x, y, text, size in spans:
        w = stringWidth(text, pdf.FONT, size)
        if x < 62 * mm:                                   # Empfangsschein
            assert x + w <= 62 * mm, text
        else:
            assert x + w <= 210 * mm, text
            assert not (67 * mm <= x <= 113 * mm and 22 * mm <= y <= 88 * mm and x + w > 113 * mm), text   # nicht in den QR-Code
    amount_y = 33 * mm
    receipt_lines = [y for x, y, t, s in spans if x < 62 * mm and t.strip() not in ("Währung", "Betrag", "CHF", "360.00", "Annahmestelle")]
    assert min(receipt_lines) > amount_y + 6                          # nichts läuft in «Währung / Betrag»


async def test_bank_files_never_book_the_same_payment_twice(app):
    """Vorher: vorgemerkte Gutschriften (PDNG) aus der Tages-Datei wurden gebucht und mit der gebuchten Zeile nochmals;
    dieselbe Zahlung ohne eigene Bankreferenz in camt.054 und im Monatsauszug camt.053 → zweimal bezahlt."""
    import test_billing_payment as tb

    from nfizyo.core.tenant import practice_scope
    from nfizyo.modules.billing import repo as billing

    inv = tb._final_invoice(app, "Lea")
    ref, amount = inv["qr_reference"], inv["total_chf"]

    def tx(r):
        return (f"<TxDtls><Amt Ccy='CHF'>{amount:.2f}</Amt><CdtDbtInd>CRDT</CdtDbtInd><RltdPties><Dbtr><Nm>Lea Muster</Nm></Dbtr></RltdPties>"
                f"<RmtInf><Strd><CdtrRefInf><Ref>{r}</Ref></CdtrRefInf></Strd></RmtInf></TxDtls>")

    def ntry(sts, acct, v08=True):
        st = f"<Sts><Cd>{sts}</Cd></Sts>" if v08 else f"<Sts>{sts}</Sts>"
        return (f"<Ntry><Amt Ccy='CHF'>{amount:.2f}</Amt><CdtDbtInd>CRDT</CdtDbtInd>{st}<BookgDt><Dt>2026-11-10</Dt></BookgDt>"
                f"<AcctSvcrRef>{acct}</AcctSvcrRef><NtryDtls>{tx(ref)}</NtryDtls></Ntry>")

    c054 = (f"<?xml version='1.0'?><Document xmlns='urn:iso:std:iso:20022:tech:xsd:camt.054.001.08'><BkToCstmrDbtCdtNtfctn><Ntfctn>"
            f"{ntry('PDNG', 'P-1')}{ntry('BOOK', 'E-1')}</Ntfctn></BkToCstmrDbtCdtNtfctn></Document>").encode()
    c053 = (f"<?xml version='1.0'?><Document xmlns='urn:iso:std:iso:20022:tech:xsd:camt.053.001.04'><BkToCstmrStmt><Stmt>"
            f"{ntry('BOOK', 'S-77', v08=False)}</Stmt></BkToCstmrStmt></Document>").encode()
    db = app[DB]
    with practice_scope(1, db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")):
        first = billing.import_payments(db, "tag.xml", c054)
        second = billing.import_payments(db, "monat.xml", c053)
    assert first["matched"] == 1 and second["matched"] == 0
    assert db.value("SELECT COUNT(*) FROM invoice_payments WHERE invoice_id = ?", (inv["id"],)) == 1


async def test_no_sms_to_landlines_reminder_goes_by_email_instead(client, app):
    """Ältere Patient/innen haben oft nur Festnetz: SMS-Erinnerung kam nie an (Praxis meinte, sie sei erinnert), Einladung
    zeigte «SMS liegt im Postausgang». Jetzt: keine SMS ans Festnetz, Erinnerung per E-Mail (mit Einverständnis), klarer Hinweis."""
    from datetime import timedelta

    from conftest import login

    from nfizyo.core.db import utcnow
    from nfizyo.core.keys import SMS
    from nfizyo.core.tenant import practice_scope
    from nfizyo.core.timeutil import now_local
    from nfizyo.modules.agenda import hooks
    from nfizyo.modules.agenda import repo as agenda

    db = app[DB]
    svc = app[SMS]
    svc.provider_of = lambda practice_id: "aspsms"          # wie live, aber ohne Netz
    svc._spawn = lambda msg_id: None
    mid = svc.send(1, "sms", "032 123 45 67", "Test")
    row = db.one("SELECT status, error FROM outbox WHERE id = ?", (mid,))
    assert row["status"] == "failed" and "Festnetz" in row["error"]
    pid = db.value("SELECT id FROM patients WHERE practice_id = 1 AND first_name = 'Lea'")
    db.execute("UPDATE patients SET phone = '032 123 45 67', email = 'lea@example.invalid', email_ok_at = ?, reminder_channel = 'sms'"
               " WHERE id = ?", (utcnow(), pid))
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    with practice_scope(1, owner):
        therapist = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
        agenda.create(db, therapist_id=therapist, start=now_local() + timedelta(hours=20), minutes=30, patient_id=pid)
    before_sms = db.value("SELECT COUNT(*) FROM outbox WHERE channel = 'sms'")
    before_mail = db.value("SELECT COUNT(*) FROM outbox WHERE channel = 'email' AND recipient = 'lea@example.invalid'")
    hooks.send_reminders(app)
    assert db.value("SELECT COUNT(*) FROM outbox WHERE channel = 'sms'") == before_sms
    assert db.value("SELECT COUNT(*) FROM outbox WHERE channel = 'email' AND recipient = 'lea@example.invalid'") == before_mail + 1
    await login(client, "empfang@demo.n-fizyo.ch")
    page = await (await client.get(f"/empfang/patienten/{pid}")).text()
    assert "SMS-Erinnerungen kommen nicht an" in page


async def test_iphone_video_is_converted_so_android_phones_can_play_it(client, app, tmp_path):
    """iPhones filmen .mov mit HEVC: auf vielen Android-Handys und in Firefox/Chrome kein Bild – die Patientin sieht nur ein
    schwarzes Feld. Jetzt wird jedes eigene Video im Hintergrund in MP4 (H.264, max. 1280 px) umgewandelt; das Original wird gelöscht."""
    import shutil
    import subprocess

    import test_homeprogram_package as hp
    from conftest import login

    from nfizyo.core.keys import JOBS

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg fehlt")
    src = tmp_path / "IMG_0001.MOV"
    enc = ["-c:v", "libx265", "-tag:v", "hvc1"] if b"libx265" in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                                                                   capture_output=True).stdout else ["-c:v", "mpeg4"]
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=720x1281:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", *enc, "-c:a", "aac", str(src)], check=True)
    db = app[DB]
    iid = hp.first_item(app, "Lea")
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post(f"/therapie/heimprogramm/{iid}/video", data=hp.upload(csrf, src.read_bytes(), name="IMG_0001.MOV"),
                          allow_redirects=False)
    assert r.status == 302
    original = db.value("SELECT video_path FROM program_items WHERE id = ?", (iid,))
    assert db.value("SELECT video_mime FROM program_items WHERE id = ?", (iid,)) == "video/quicktime"
    await app[JOBS].run_all(app)
    row = db.one("SELECT video_path, video_mime, video_size, video_consent_at FROM program_items WHERE id = ?", (iid,))
    assert row["video_mime"] == "video/mp4" and row["video_path"].endswith("-web.mp4") and row["video_consent_at"]
    assert not Path(original).exists()
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
                            "-of", "csv=p=0", row["video_path"]], capture_output=True, text=True).stdout.strip()
    codec, w, h = probe.split(",")
    assert codec == "h264" and int(h) <= 1280 and int(w) % 2 == 0 and int(h) % 2 == 0
    r = await client.get(f"/therapie/heimprogramm/{iid}/video")
    assert r.status == 200 and r.headers["Content-Type"] == "video/mp4"


async def test_long_background_work_runs_beside_dictations_not_before_them(app):
    """Ein Video umwandeln oder 20 Scans lesen dauert Minuten. Mit nur einem Arbeiter wartete das gerade aufgenommene Diktat
    so lange. Jetzt zwei Arbeiter: Hintergrund und «jemand wartet» laufen nebeneinander."""
    import asyncio

    from nfizyo.core.keys import JOBS

    jobs = app[JOBS]
    log = []

    async def slow(app_, practice_id, ref_id):
        log.append("video start")
        await asyncio.sleep(0.6)
        log.append("video done")

    async def quick(app_, practice_id, ref_id):
        log.append("diktat done")

    jobs.register("video_convert", slow, background=True)
    jobs.register("transcribe", quick)
    await jobs.start(app)
    try:
        jobs.enqueue(1, "video_convert", 1)
        await asyncio.sleep(0.1)
        jobs.enqueue(1, "transcribe", 2)
        for _ in range(40):
            if "video done" in log:
                break
            await asyncio.sleep(0.05)
    finally:
        await jobs.stop(app)
    assert log == ["video start", "diktat done", "video done"]


async def test_invoice_finalized_twice_gets_one_number(client, app):
    """Doppelklick / zweites Fenster: dieselbe Rechnung zweimal abschliessen → eine Nummer, ein Protokolleintrag."""
    from nfizyo.core.keys import DB
    from nfizyo.core.tenant import practice_scope
    from nfizyo.modules.billing import repo as brepo
    from nfizyo.modules.firms import repo as frepo
    from conftest import pid_of

    db = app[DB]
    pid = 1
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    with practice_scope(pid, owner):
        brepo.set_tp_value(db, "kvg312", 1.0, "2026-01-01")
        patient = db.one("SELECT * FROM patients WHERE id = ?", (pid_of(app, "Peter"),))
        draft = {"id": brepo.create_from_appointments(db, patient, "de")}
        stale = brepo.get(db, draft["id"])
        first = brepo.finalize(db, stale)
        assert first and brepo.finalize(db, stale) is None             # alter Stand im zweiten Fenster
        assert db.value("SELECT COUNT(*) FROM audit_log WHERE action = 'finalize' AND entity = 'invoice' AND entity_id = ?",
                        (draft["id"],)) == 1
        fdraft = db.one("SELECT id FROM firm_invoices WHERE practice_id = ? AND status = 'draft' ORDER BY id LIMIT 1", (pid,))
        if fdraft is not None:
            fstale = frepo.get(db, fdraft["id"])
            if frepo.finalize(db, fstale):
                assert frepo.finalize(db, fstale) is None


async def test_stale_master_data_form_does_not_revert_other_changes(client, app):
    """Zwei offene Formulare: Empfang A ändert die Adresse, inzwischen hat B die Telefonnummer und der Posteingang die
    Versicherung geändert → A setzt Telefon und Versicherung nicht still zurück."""
    import html as _html
    import re as _re

    from conftest import csrf_of, login, pid_of
    from nfizyo.core.keys import DB

    db = app[DB]
    pid = pid_of(app, "Peter")
    await login(client, "empfang@demo.n-fizyo.ch")
    page = await (await client.get(f"/empfang/patienten/{pid}")).text()
    form = _re.search(r'<form method="post" action="/empfang/patienten/%d" class="stack">(.*?)</form>' % pid, page, _re.S).group(1)
    fields: dict = {}
    for name, value in _re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form):
        fields.setdefault(name, _html.unescape(value))
    for name, body in _re.findall(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', form, _re.S):
        sel = _re.search(r'<option value="([^"]*)"[^>]*selected', body)
        fields[name] = sel.group(1) if sel else ""
    for name, body in _re.findall(r'<textarea[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', form, _re.S):
        fields[name] = _html.unescape(body)
    assert "_orig" in fields
    other_insurer = db.value("SELECT id FROM insurers WHERE practice_id = 1 AND id != COALESCE((SELECT insurer_id FROM patients WHERE id = ?), 0)"
                             " ORDER BY id LIMIT 1", (pid,))
    db.execute("UPDATE patients SET phone = '+41791112233', insurer_id = ? WHERE id = ?", (other_insurer, pid))   # B / Posteingang
    fields["city"] = "Biel/Bienne"
    fields["_csrf"] = csrf_of(page)
    r = await client.post(f"/empfang/patienten/{pid}", data=fields, allow_redirects=False)
    assert r.status == 302
    row = db.one("SELECT phone, insurer_id, city FROM patients WHERE id = ?", (pid,))
    assert row["city"] == "Biel/Bienne"
    assert row["phone"] == "+41791112233" and row["insurer_id"] == other_insurer
    # ohne Ausgangsstand (alte Seite im Browser): wie bisher
    fields.pop("_orig")
    fields["city"] = "Nidau"
    await client.post(f"/empfang/patienten/{pid}", data=fields, allow_redirects=False)
    assert db.value("SELECT city FROM patients WHERE id = ?", (pid,)) == "Nidau"


def _done(db, patient_id, therapist_id, type_id, start, minutes):
    from datetime import datetime, timedelta
    s = datetime.fromisoformat(start)
    return db.execute("INSERT INTO appointments (practice_id, patient_id, therapist_id, treatment_type_id, starts_at, ends_at, status,"
                      " created_at, updated_at) VALUES (1,?,?,?,?,?,'done',?,?)",
                      (patient_id, therapist_id, type_id, s.isoformat(), (s + timedelta(minutes=minutes)).isoformat(), s.isoformat(),
                       s.isoformat()))


async def test_uv_tariff_follows_treatment_type_and_group_divisor(client, app):
    """UV/MV/IV: MTT → 25.520 (Sitzung), Hippotherapie → 25.120, Gruppe → 25.410 nach Divisormethode (höchstens 75 Min.);
    bisher ging jede UV-Behandlung als 25.110 pro 5 Minuten hinaus (MTT und Gruppe viel zu teuer)."""
    from conftest import pid_of
    from nfizyo.core.keys import DB
    from nfizyo.core.tenant import practice_scope
    from nfizyo.modules.billing import repo as brepo

    db = app[DB]
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    ther = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    lea, luca, yves = pid_of(app, "Lea"), pid_of(app, "Luca"), pid_of(app, "Yves")
    with practice_scope(1, owner):
        db.execute("UPDATE appointments SET status = 'cancelled' WHERE patient_id IN (?,?,?) AND status = 'done'", (lea, luca, yves))
        hippo = db.execute("INSERT INTO treatment_types (practice_id, name_de, name_fr, minutes, tariff_code) VALUES (1,'Hippo','Hippo',45,'7313')")
        group = db.execute("INSERT INTO treatment_types (practice_id, name_de, name_fr, minutes, tariff_code) VALUES (1,'Gruppe','Groupe',60,'7330')")
        mtt = db.value("SELECT id FROM treatment_types WHERE practice_id = 1 AND tariff_code = '7340'")
        _done(db, lea, ther, mtt, "2026-11-02T08:00:00+00:00", 60)
        _done(db, lea, ther, hippo, "2026-11-03T08:00:00+00:00", 45)
        for p in (lea, luca, yves):                                    # Gruppe: 3 Anwesende, 90 Min. → höchstens 75 Min.
            _done(db, p, ther, group, "2026-11-04T08:00:00+00:00", 90)
        iid = brepo.create_from_appointments(db, db.one("SELECT * FROM patients WHERE id = ?", (lea,)), "de")
        lines = {ln["code"]: ln for ln in db.query("SELECT * FROM invoice_lines WHERE invoice_id = ?", (iid,))}
        assert set(lines) == {"25.520", "25.120", "25.410"}
        assert lines["25.520"]["quantity"] == 1.0                      # MTT-Training: pro Sitzung
        assert lines["25.120"]["quantity"] == 9.0                      # 45 Min. Hippotherapie
        assert lines["25.410"]["quantity"] == 5.0                      # 75 Min. = 15 Einheiten / 3 Anwesende
        assert abs(lines["25.410"]["amount_chf"] - round(5 * 10.38 * 0.94, 2)) < 0.001
        warn = brepo.warnings(db, brepo.get(db, iid), db.query("SELECT * FROM invoice_lines WHERE invoice_id = ?", (iid,)))
        assert "group_divided" in warn and "max_minutes" in warn


async def test_new_kvg_structure_ready_as_data(client, app, monkeypatch):
    """Neue KVG-Struktur (5-Minuten-Positionen): sobald sie als Daten eingetragen ist, rechnet N-Fizyo ab dem Stichtag damit –
    Behandlungsart → Position, Limitation, Kennzeichnung je Behandlung, Trennung an der Grenze, Taxpunktwert-Seite."""
    import dataclasses

    from conftest import login, pid_of
    from nfizyo.core.keys import DB
    from nfizyo.core.tenant import practice_scope
    from nfizyo.knowledge import tariffs as cat
    from nfizyo.modules.billing import repo as brepo

    new = cat.Tariff("kvg2027", ("KVG",), "KVG neu (Test)", "LAMal nouveau (test)", "2026-11-05", None, "Test", code="312",
                     default_code="27110", remote_code="27610", session_map=(("7340", "27320"),), marker_code="27100")
    monkeypatch.setitem(cat.TARIFFS, "kvg2027", new)
    monkeypatch.setitem(cat.TARIFFS, "kvg312", dataclasses.replace(cat.TARIFFS["kvg312"], valid_until="2026-11-04"))
    for p in (cat.Position("kvg2027", "27110", "Einzelbehandlung, pro 5 Min.", "Individuel, par 5 min", 7.36, "5min", max_minutes=50),
              cat.Position("kvg2027", "27320", "MTT-Training", "MTT", 27.93, "session"),
              cat.Position("kvg2027", "27610", "Video, pro 5 Min.", "Vidéo, par 5 min", 7.19, "5min", max_minutes=30),
              cat.Position("kvg2027", "27100", "Kennzeichnung Behandlung", "Marqueur", 0, "flat", group="supplement")):
        monkeypatch.setitem(cat.BY_CODE, ("kvg2027", p.code), p)
    db = app[DB]
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    ther = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    hans = pid_of(app, "Hans")
    with practice_scope(1, owner):
        brepo.set_tp_value(db, "kvg312", 1.0, "2018-01-01")
        brepo.set_tp_value(db, "kvg2027", 1.1, "2026-11-05")
        db.execute("UPDATE appointments SET status = 'cancelled' WHERE patient_id = ? AND status = 'done'", (hans,))
        mtt = db.value("SELECT id FROM treatment_types WHERE practice_id = 1 AND tariff_code = '7340'")
        _done(db, hans, ther, 1, "2026-11-03T08:00:00+00:00", 30)        # vor dem Stichtag: 312
        _done(db, hans, ther, 1, "2026-11-06T08:00:00+00:00", 60)        # danach: 27110, höchstens 50 Min.
        _done(db, hans, ther, mtt, "2026-11-09T08:00:00+00:00", 60)      # MTT → 27320
        first = brepo.create_from_appointments(db, db.one("SELECT * FROM patients WHERE id = ?", (hans,)), "de")
        invs = db.query("SELECT id, tariff FROM invoices WHERE patient_id = ? AND status = 'draft' ORDER BY id", (hans,))
        assert first and {i["tariff"] for i in invs} == {"kvg312", "kvg2027"}       # an der Grenze getrennt
        new_id = next(i["id"] for i in invs if i["tariff"] == "kvg2027")
        lines = db.query("SELECT code, quantity, amount_chf FROM invoice_lines WHERE invoice_id = ? ORDER BY id", (new_id,))
        codes = [ln["code"] for ln in lines]
        assert codes.count("27100") == 2 and "27320" in codes
        t110 = next(ln for ln in lines if ln["code"] == "27110")
        assert t110["quantity"] == 10.0 and abs(t110["amount_chf"] - round(10 * 7.36 * 1.1, 2)) < 0.001
        assert all(ln["amount_chf"] == 0 for ln in lines if ln["code"] == "27100")
    # Taxpunktwert-Seite zeigt beide KVG-Tarife zur Auswahl
    await login(client, "leitung@demo.n-fizyo.ch")
    page = await (await client.get("/leitung/einstellungen/tarife")).text()
    assert 'value="kvg2027"' in page and "KVG neu (Test)" in page


def test_import_day_one_details(app):
    """Datenübernahme am ersten Pilottag: Gesetz aus dem Versicherer, zweiter Lauf ohne Geburtsdatum legt nichts doppelt an,
    Statustexte anderer Programme, unbekannter Status wird nicht geraten, ZSR wird bereinigt."""
    from nfizyo.core.keys import DB
    from nfizyo.core.tenant import practice_scope
    from nfizyo.modules.dataimport import engine

    db = app[DB]
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    with practice_scope(1, owner):
        ins = db.one("SELECT name, law FROM insurers WHERE practice_id = 1 AND law = 'UVG' LIMIT 1")
        h = ["Name", "Vorname", "Versicherung"]
        m = engine.guess_mapping("patients", h)
        rows = engine.check(db, "patients", [["Neuer", "Kunde", ins["name"]], ["Muster", "Peter", ""]], m)
        assert rows[0].values["law"] == "UVG" and "law_from_insurer" in rows[0].notes
        assert rows[1].status == "duplicate" and "dup_name" in rows[1].notes      # Peter Muster gibt es schon, kein Geburtsdatum

        ther = db.one("SELECT name FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")["name"]
        h = ["Name", "Vorname", "Datum", "Beginn", "Therapeut", "Status"]
        m = engine.guess_mapping("appointments", h)

        def status(text):
            r = engine.check(db, "appointments", [["Muster", "Peter", "20.01.2027", "08:00", ther, text]], m)[0]
            return r.status, r.values.get("status"), r.notes

        assert status("abgesagt (Patient)")[1] == "cancelled"
        assert status("Termin verschoben")[1] == "cancelled"
        assert status("Nicht erschienen")[1] == "no_show"
        assert status("bestätigt")[1] == "booked"
        assert status("")[1] == "booked"                                          # leer: Zukunft = gebucht
        assert status("XYZ-7")[0] == "error" and status("XYZ-7")[2] == ["unknown_status"]

        h = ["Name", "ZSR"]
        m = engine.guess_mapping("referrers", h)
        rows = engine.check(db, "referrers", [["Dr. Neu", "H 1234.56"], ["Dr. Alt", "123"]], m)
        assert rows[0].values["zsr"] == "H123456" and rows[1].values["zsr"] is None and "bad_zsr" in rows[1].notes


def test_moved_appointment_gets_new_reminder_and_quiet_hours(app):
    """Verschobener Termin nach der Erinnerung: die neue Zeit wird wieder erinnert. Keine Erinnerungen zwischen 21 und 7 Uhr."""
    from nfizyo.core.keys import DB
    from nfizyo.core.tenant import practice_scope
    from nfizyo.modules.agenda import hooks
    from nfizyo.modules.agenda import repo as arepo

    db = app[DB]
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    with practice_scope(1, owner):
        a = db.one("SELECT id, starts_at, ends_at FROM appointments WHERE practice_id = 1 AND status = 'booked' AND patient_id IS NOT NULL LIMIT 1")
        arepo.mark_reminded(db, 1, a["id"])
        arepo.update(db, a["id"], note="nur Notiz")
        assert db.value("SELECT reminder_sent_at FROM appointments WHERE id = ?", (a["id"],))      # Notiz: bleibt erinnert
        arepo.update(db, a["id"], starts_at=a["starts_at"].replace("T", "T").replace(":00:00", ":30:00") if ":00:00" in a["starts_at"]
                     else a["starts_at"][:14] + "05" + a["starts_at"][16:])
        assert db.value("SELECT reminder_sent_at FROM appointments WHERE id = ?", (a["id"],)) is None
    assert [h for h in range(24) if hooks.quiet_hours_over(h)] == list(range(7, 21))


def test_cash_storno_on_invoice_removes_draft_or_refuses(app):
    """Kasse «auf Rechnung» stornieren: Rechnungsentwurf wird gelöscht (sonst würde der stornierte Verkauf später verrechnet);
    ist die Rechnung schon abgeschlossen → Storno abgelehnt, nichts geändert. Doppelklick: ein Protokolleintrag."""
    from conftest import pid_of
    from nfizyo.core.keys import DB
    from nfizyo.core.tenant import practice_scope
    from nfizyo.core.timeutil import today
    from nfizyo.modules.billing import private
    from nfizyo.modules.billing import repo as brepo
    from nfizyo.modules.cashdesk import repo as crepo

    db = app[DB]
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    with practice_scope(1, owner):
        it = private.item(db, private.save_item(db, {"kind": "product", "code": "ST-1", "name_de": "Tape", "name_fr": "Tape",
                                                     "price_chf": 12.0, "vat_rate": 0.0}))
        patient = db.one("SELECT * FROM patients WHERE id = ?", (pid_of(app, "Peter"),))
        day = today().isoformat()
        sid, iid = crepo.sell(db, patient=patient, entries=[(it, 1)], method="invoice", lang="de", day=day)
        assert brepo.get(db, iid)["status"] == "draft"
        sale = crepo.get(db, sid)
        assert crepo.cancel(db, sale, "falsch erfasst") is True
        assert brepo.get(db, iid) is None
        assert crepo.cancel(db, sale, "nochmals") is False                       # alter Stand im zweiten Fenster
        assert db.value("SELECT COUNT(*) FROM audit_log WHERE action = 'cancel' AND entity = 'cash_sale' AND entity_id = ?", (sid,)) == 1
        sid2, iid2 = crepo.sell(db, patient=patient, entries=[(it, 1)], method="invoice", lang="de", day=day)
        brepo.finalize(db, brepo.get(db, iid2))
        assert crepo.cancel(db, crepo.get(db, sid2), "zu spät") == "invoice_final"
        assert crepo.get(db, sid2)["status"] == "invoiced" and brepo.get(db, iid2)["status"] == "final"


async def test_new_patient_warns_about_existing_person(client, app):
    """Am Telefon dieselbe Person zweimal anlegen: zuerst Hinweis mit Link zur bestehenden Person, erst nach Bestätigung neu."""
    from conftest import csrf_of, login
    from nfizyo.core.keys import DB

    db = app[DB]
    p = db.one("SELECT * FROM patients WHERE first_name = 'Peter' AND practice_id = 1")
    await login(client, "empfang@demo.n-fizyo.ch")
    page = await (await client.get("/empfang/patienten/neu")).text()
    data = {"_csrf": csrf_of(page), "first_name": "peter", "last_name": p["last_name"].upper(), "birthdate": p["birthdate"] or "",
            "lang": "de"}
    n = db.value("SELECT COUNT(*) FROM patients")
    r = await client.post("/empfang/patienten/neu", data=data, allow_redirects=False)
    assert r.status == 200 and f'/empfang/patienten/{p["id"]}"' in await r.text()
    assert db.value("SELECT COUNT(*) FROM patients") == n
    r = await client.post("/empfang/patienten/neu", data={**data, "confirm_new": "1"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT COUNT(*) FROM patients") == n + 1


async def test_cancel_appointment_can_inform_patient_by_sms(client, app):
    """Praxis sagt einen künftigen Termin ab → Häkchen «per SMS informieren» (Handynummer), SMS in der Sprache der Person."""
    from conftest import csrf_of, login
    from nfizyo.core.db import utcnow as now_iso
    from nfizyo.core.keys import DB

    db = app[DB]
    a = db.one("SELECT a.id, p.id AS pid FROM appointments a JOIN patients p ON p.id = a.patient_id WHERE a.practice_id = 1"
               " AND a.status = 'booked' AND a.starts_at > ? ORDER BY a.starts_at LIMIT 1", (now_iso(),))
    db.execute("UPDATE patients SET phone = '+41791234567', lang = 'fr' WHERE id = ?", (a["pid"],))
    await login(client, "empfang@demo.n-fizyo.ch")
    page = await (await client.get(f"/empfang/agenda/termine/{a['id']}")).text()
    assert 'name="notify"' in page
    n = db.value("SELECT COUNT(*) FROM outbox WHERE recipient = '+41791234567'")
    r = await client.post(f"/empfang/agenda/termine/{a['id']}/status",
                          data={"_csrf": csrf_of(page), "status": "cancelled", "reason": "Krank", "notify": "1"}, allow_redirects=False)
    assert r.status == 302
    sms = db.one("SELECT * FROM outbox WHERE recipient = '+41791234567' ORDER BY id DESC LIMIT 1")
    assert db.value("SELECT COUNT(*) FROM outbox WHERE recipient = '+41791234567'") == n + 1 and "annul" in sms["body"].lower()
    db.execute("UPDATE patients SET phone = '032 123 45 67' WHERE id = ?", (a["pid"],))           # Festnetz: kein Häkchen
    b = db.value("SELECT id FROM appointments WHERE patient_id = ? AND status = 'booked' AND starts_at > ? LIMIT 1",
                 (a["pid"], now_iso()))
    if b:
        assert 'name="notify"' not in await (await client.get(f"/empfang/agenda/termine/{b}")).text()


async def test_note_two_windows_no_silent_overwrite(client, app):
    """Notiz in zwei Fenstern: altes Fenster speichert → Hinweis, die andere Fassung bleibt im Verlauf. Entwurf inzwischen
    freigegeben → der getippte Text geht nicht verloren, er steht als Nachtrag bereit."""
    import re as _re

    from conftest import csrf_of, login, pid_of
    from nfizyo.core.keys import DB
    from nfizyo.core.tenant import practice_scope
    from nfizyo.modules.documentation import repo as nrepo

    db = app[DB]
    ther = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    pid = db.value("SELECT patient_id FROM patient_team WHERE user_id = ? LIMIT 1", (ther,)) or pid_of(app, "Peter")
    with practice_scope(1, ther):
        nid = nrepo.create(db, pid, {"befund": "A"})
    await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get(f"/therapie/notizen/{nid}")).text()
    assert 'name="base" value="1"' in page
    csrf = csrf_of(page)
    with practice_scope(1, ther):
        nrepo.save(db, nid, {"befund": "B aus Fenster 2"})                       # Fenster 2 speichert Version 2
    r = await client.post(f"/therapie/notizen/{nid}", data={"_csrf": csrf, "base": "1", "befund": "C aus Fenster 1"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT COUNT(*) FROM note_versions WHERE note_id = ?", (nid,)) == 3
    flash = await (await client.get(r.headers["Location"])).text()
    assert "Verlauf" in flash
    with practice_scope(1, ther):
        nrepo.approve(db, nid)                                                   # inzwischen freigegeben
    r = await client.post(f"/therapie/notizen/{nid}", data={"_csrf": csrf, "base": "3", "befund": "D wichtig"}, allow_redirects=False)
    body = await r.text()
    assert r.status == 200 and "D wichtig" in body and _re.search(r"<details class=\"card\" open", body)


async def test_patient_messages_capped(client, app):
    """Hasta uygulaması: sehr lange Nachricht wird gekürzt, Schmerz > 10 ignoriert, mehr als 20 Nachrichten pro Stunde → Hinweis
    (sonst stünden Übersetzungen vor den Diktaten in der Warteschlange)."""
    from conftest import csrf_of, pid_of
    from nfizyo.core.auth import create_session
    from nfizyo.core.keys import DB
    from nfizyo.modules.messages import repo as mrepo

    db = app[DB]
    pid = pid_of(app, "Lea")
    token, _ = create_session(db, "patient", patient_id=pid, practice_id=1)
    client.session.cookie_jar.update_cookies({"nf_p": token})
    csrf = csrf_of(await (await client.get("/p/nachrichten")).text())
    await client.post("/p/nachrichten", data={"_csrf": csrf, "body": "x" * 50000})
    assert len(db.value("SELECT body FROM messages WHERE patient_id = ? ORDER BY id DESC LIMIT 1", (pid,))) == mrepo.MAX_BODY
    await client.post("/p/melden", data={"_csrf": csrf, "pain": "99", "text": "Knie"})
    assert db.value("SELECT pain FROM messages WHERE patient_id = ? ORDER BY id DESC LIMIT 1", (pid,)) is None
    for i in range(mrepo.MAX_PER_HOUR):
        await client.post("/p/nachrichten", data={"_csrf": csrf, "body": f"Nachricht {i}"})
    n = db.value("SELECT COUNT(*) FROM messages WHERE patient_id = ? AND direction = 'in'", (pid,))
    r = await client.post("/p/nachrichten", data={"_csrf": csrf, "body": "noch eine"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT COUNT(*) FROM messages WHERE patient_id = ? AND direction = 'in'", (pid,)) == n


def test_mtt_numbers_plausible():
    """MTT-Eingaben: 30 Stellen (Absturz beim Speichern), «inf»/«1e300» (verdirbt die Laststeigerung), negative und
    unsinnig hohe Werte → leer statt gespeichert."""
    from nfizyo.modules.mtt.views import _num

    assert _num("9" * 30) is None and _num("inf", float) is None and _num("nan", float) is None and _num("1e300", float) is None
    assert _num("-5") is None and _num("600", limit=500) is None
    assert _num("12,5", float, 500) == 12.5 and _num(" 8 ") == 8 and _num("123456", limit=10**12) == 123456


async def test_intake_limits(client, app, monkeypatch):
    """Anliegen aus der App: höchstens 5 pro Tag; Sprachaufnahme über der Grenze → 413, nichts bleibt liegen."""
    import aiohttp

    from conftest import csrf_of, pid_of
    from nfizyo.core.auth import create_session
    from nfizyo.core.keys import AI, DB
    from nfizyo.modules.intake import views as iviews

    db = app[DB]
    pid = pid_of(app, "Lea")
    token, _ = create_session(db, "patient", patient_id=pid, practice_id=1)
    client.session.cookie_jar.update_cookies({"nf_p": token})
    csrf = csrf_of(await (await client.get("/p/anliegen")).text())
    before = db.value("SELECT COUNT(*) FROM intakes WHERE patient_id = ?", (pid,))
    for _ in range(iviews.MAX_PER_DAY + 2):
        await client.post("/p/anliegen", data={"_csrf": csrf, "pain": "3"})
    assert db.value("SELECT COUNT(*) FROM intakes WHERE patient_id = ?", (pid,)) - before <= iviews.MAX_PER_DAY
    monkeypatch.setattr(iviews, "MAX_AUDIO", 1000)
    monkeypatch.setattr(app[AI], "can_transcribe", lambda: True)
    fd = aiohttp.FormData()
    fd.add_field("audio", b"x" * 5000, filename="stimme", content_type="audio/webm")
    r = await client.post("/p/anliegen/aufnahme", data=fd, headers={"X-CSRF-Token": csrf})
    assert r.status == 413


# --------------------------------------------------------------------------- Mobil-Runde (14.11.2026): untere Leiste, Körperfigur
def test_bottom_bar_short_labels_exist_in_every_language():
    """Kurze Beschriftungen der unteren Leiste: Personal in DE/FR, Patienten-App in allen 11 Sprachen, nie länger als 10 Zeichen."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "nfizyo"

    def get(path, dotted):
        cur = json.loads(path.read_text(encoding="utf-8"))
        for k in dotted.split("."):
            cur = cur[k]
        return cur

    staff = {"core/locales": ["nav.inbox_short", "nav.today_short", "nav.waitlist_short", "nav.overview_short", "nav.messages_short"],
             "modules/rxwatch/locales": ["rx.nav_short"], "modules/billing/locales": ["nav.billing_short"],
             "modules/firms/locales": ["nav.firms_short"]}
    for folder, keys in staff.items():
        for lang in ("de", "fr"):
            for key in keys:
                assert 0 < len(get(root / folder / f"{lang}.json", key)) <= 10, (folder, lang, key)
    for lang in ("de", "fr", "it", "en", "es", "pt", "sq", "bks", "tr", "uk", "ar"):
        for key in ("p.nav.today_short", "p.nav.appointments_short", "p.nav.messages_short"):
            assert 0 < len(get(root / "modules/patientapp/locales" / f"{lang}.json", key)) <= 10, (lang, key)
        assert get(root / "core/locales" / f"{lang}.json", "p.body.zoom")
    assert "t(n.short_key)" in (root / "web/templates/layouts/patient.html").read_text(encoding="utf-8")


def test_body_figure_has_zoom_button_only_when_editable():
    from pathlib import Path

    fig =(Path(__file__).resolve().parent.parent / "nfizyo/web/templates/ui/figure.html").read_text(encoding="utf-8")
    assert "{% if editable %}<button type=\"button\" class=\"icon-btn bf-zoom\" data-bf-zoom" in fig
    js = (Path(__file__).resolve().parent.parent / "nfizyo/web/static/js/app.js").read_text(encoding="utf-8")
    assert "classList.toggle('zoomed')" in js


# --------------------------------------------------------------------------- GitHub-Deploy (18.11.2026): eigener Server, eigene Secrets
def test_deploy_workflow_and_bootstrap_stay_narrow():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    wf = (root / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "workflow_run" in wf and "conclusion == 'success'" in wf            # nur nach grünen Tests
    assert "git archive" in wf and "upload-artifact" not in wf                 # kein Artefakt-Kontingent
    assert "StrictHostKeyChecking=yes" in wf
    assert "NGASTRO" not in wf                                                   # nie die Schlüssel von N-Gastro
    for s in ("FIZYO_SSH_HOST", "FIZYO_SSH_KEY", "FIZYO_SSH_KNOWN_HOSTS"):
        assert f"secrets.{s}" in wf
    boot = (root / "deploy/server-bootstrap.sh").read_text(encoding="utf-8")
    assert "printf 'restrict %s\\n'" in boot                                      # Schlüssel ohne Terminal/Weiterleitung
    assert 'NOPASSWD: /usr/local/sbin/n-fizyo-deploy ""' in boot                  # sudo nur dieses Programm, ohne Argumente
    assert "update.sh\" --force" in boot and "install.sh\" --demo --no-ai" in boot


async def test_public_site_only_on_demo_server(aiohttp_client, app, tmp_path):
    """nfizyo.ch zeigt Besuchern die Vorstellungsseite; angemeldet geht es weiter in die Praxis.
    Eine echte Praxis (kein Demo) zeigt unter «/» nie eine Werbeseite, sondern die Anmeldung."""
    from conftest import login

    c = await aiohttp_client(app)
    r = await c.get("/", allow_redirects=False)
    html = await r.text()
    assert r.status == 200 and "Mehr Therapie." in html and 'href="/login"' in html and "fiktive Daten" in html
    assert "noindex" not in html and "<script" not in html
    await c.get("/lang/fr?next=/")
    html = await (await c.get("/")).text()
    assert 'lang="fr"' in html and "Plus de thérapie." in html and "heute-fr.webp" in html
    await login(c, "therapie@demo.n-fizyo.ch")
    r = await c.get("/", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"].startswith("/therapie")

    real = create_app(load_settings(None, data_dir=tmp_path / "echt", secret_key=b"t" * 32, demo=False), start_workers=False)
    c2 = await aiohttp_client(real)
    r = await c2.get("/", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"].startswith("/login")
    off = create_app(load_settings(None, data_dir=tmp_path / "aus", secret_key=b"t" * 32, demo=True, public_site=False), start_workers=False)
    r = await (await aiohttp_client(off)).get("/", allow_redirects=False)
    assert r.status == 302


def test_site_texts_complete_and_without_superlatives():
    from nfizyo.core.site_content import SITE

    def shape(d):
        return {k: (shape(v) if isinstance(v, dict) else len(v) if isinstance(v, list) else None) for k, v in d.items()}

    assert shape(SITE["de"]) == shape(SITE["fr"])
    blob = repr(SITE).lower()
    for word in ("erste ", "einzige", "premier logiciel", "seul ", "unique ", "le seul", "beste", "meilleur", "garantiert"):
        assert word not in blob, word
    root = Path(__file__).resolve().parents[1] / "nfizyo" / "web" / "static" / "img"
    for lang in SITE:
        for f in SITE[lang]["features"]:
            assert (root / "site" / f"{f['img']}-{lang}.webp").is_file()
    assert (root / "n.svg").is_file()
