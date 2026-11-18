"""HIN: Bericht als PDF senden (nur abgeschlossen, nur mit HIN-Adresse), eingehende HIN-Mails im Posteingang der richtigen Praxis."""
from __future__ import annotations

from email.message import EmailMessage

import pytest
from conftest import csrf_of, login, pid_of

from nfizyo.app import create_app
from nfizyo.core import hin
from nfizyo.core.config import load_settings
from nfizyo.core.keys import DB, HIN
from nfizyo.core.tenant import practice_scope


@pytest.fixture
def hin_app(tmp_path):
    settings = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True, hin_practice="demo",
                             hin_from="praxis@demo-hin.invalid", hin_user="praxis", hin_password="geheim")
    return create_app(settings, start_workers=False)


def _final_report(app, first_name="Lea"):
    from nfizyo.modules.reports import repo as reports

    db = app[DB]
    pid = pid_of(app, first_name)
    uid = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    ref = db.value("SELECT id FROM referrers WHERE practice_id = 1 AND hin_email IS NOT NULL LIMIT 1")
    with practice_scope(1, uid):
        rid = reports.create(db, pid, kind="progress", lang="de", referrer_id=ref, prescription_id=None, subject="Verlauf",
                             salutation="Sehr geehrte Damen und Herren", sections={"assessment": "Gute Fortschritte."})
        reports.finalize(db, rid)
    return rid


def test_report_pdf_is_valid_and_has_unicode():
    from pypdf import PdfReader
    from io import BytesIO

    from nfizyo.modules.reports import pdf

    r = {"lang": "de", "kind": "progress", "subject": "Ayşe Yılmaz, 01.02.1980", "salutation": "Sehr geehrte Frau Kollegin",
         "diagnosis": "Status nach VKB-Plastik", "course": "", "findings": "", "assessment": "Gut – «stabil»", "recommendation": "",
         "finalized_at": "2026-10-28T10:00:00+00:00", "updated_at": "2026-10-28T10:00:00+00:00", "referrer_name": "Dr. med. P. Beispiel",
         "referrer_practice": None, "referrer_address": "Hauptstrasse 1\n2500 Biel", "first_name": "Ayşe", "last_name": "Yılmaz"}
    practice = {"name": "Physio Demo", "address": "Demostrasse 1", "phone": "032", "email": None, "gln": "7601000000000", "zsr": None}
    data = pdf.build(r, practice, {"name": "Luca E.", "gln": None}, lambda k, **kw: k.split(".")[-1], demo=True)
    assert data.startswith(b"%PDF")
    text = PdfReader(BytesIO(data)).pages[0].extract_text()
    assert "Ayşe Yılmaz" in text and "«stabil»" in text


async def test_send_report_over_hin(aiohttp_client, hin_app, monkeypatch):
    sent = []

    def fake_send(to, subject, body, attachments):
        sent.append((to, subject, body, attachments))
        return "<id@hin>"

    monkeypatch.setattr(hin_app[HIN], "send", fake_send)
    rid = _final_report(hin_app)
    client = await aiohttp_client(hin_app)
    await login(client, "therapie@demo.n-fizyo.ch")
    page = await client.get(f"/therapie/berichte/{rid}")
    html = await page.text()
    assert "Über HIN senden" in html
    r = await client.post(f"/therapie/berichte/{rid}/hin", data={"_csrf": csrf_of(html)}, allow_redirects=False)
    assert r.status == 302
    to, subject, _, attachments = sent[0]
    assert to.endswith("@demo-hin.invalid") and "Lea Muster" in subject
    assert attachments[0][1].startswith(b"%PDF") and attachments[0][2] == "application/pdf"
    db = hin_app[DB]
    assert db.one("SELECT status, sent_via FROM reports WHERE id = ?", (rid,))["sent_via"] == "hin"
    out = db.one("SELECT * FROM outbox WHERE channel = 'hin'")
    assert out["status"] == "sent" and out["body"] == f"Bericht #{rid}"  # kein Inhalt im Postausgang


async def test_hin_failure_does_not_mark_sent(aiohttp_client, hin_app, monkeypatch):
    def broken(*a, **k):
        raise ConnectionRefusedError("HIN Client aus")

    monkeypatch.setattr(hin_app[HIN], "send", broken)
    rid = _final_report(hin_app)
    client = await aiohttp_client(hin_app)
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    await client.post(f"/therapie/berichte/{rid}/hin", data={"_csrf": csrf}, allow_redirects=False)
    db = hin_app[DB]
    assert db.one("SELECT status FROM reports WHERE id = ?", (rid,))["status"] == "final"
    assert db.one("SELECT status FROM outbox WHERE channel = 'hin'")["status"] == "failed"


async def test_no_hin_button_without_configuration(client, app):
    rid = _final_report(app)
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    html = await (await client.get(f"/therapie/berichte/{rid}")).text()
    assert "Über HIN senden" not in html
    await client.post(f"/therapie/berichte/{rid}/hin", data={"_csrf": csrf}, allow_redirects=False)
    assert app[DB].one("SELECT status FROM reports WHERE id = ?", (rid,))["status"] == "final"


def _mail(subject, attachment=None, text="Siehe Anhang"):
    m = EmailMessage()
    m["From"] = "Dr. Beispiel <ortho@demo-hin.invalid>"
    m["To"] = "praxis@demo-hin.invalid"
    m["Subject"] = subject
    m.set_content(text)
    if attachment:
        m.add_attachment(attachment[1], maintype="application", subtype="pdf", filename=attachment[0])
    return m.as_bytes()


async def test_incoming_hin_lands_in_own_practice_only(hin_app, monkeypatch):
    from nfizyo.core.db import utcnow
    from nfizyo.modules.inbox.views import hin_poll_once

    hin_app[DB].execute("INSERT INTO practices (name, slug, default_lang, created_at) VALUES (?,?,?,?)", ("Praxis B", "praxis-b", "fr", utcnow()))

    mails = [hin.parse("11", _mail("OP-Bericht Muster Lea", ("op.pdf", b"%PDF-1.4 fake op report"))),
             hin.parse("12", _mail("Kurze Mitteilung", None, "Bitte Lea Muster ab nächster Woche belasten."))]
    seen = []
    monkeypatch.setattr(hin_app[HIN], "fetch_unseen", lambda: mails)
    monkeypatch.setattr(hin_app[HIN], "mark_seen", lambda uids: seen.extend(uids))
    n = await hin_poll_once(hin_app)
    assert n == 2 and seen == ["11", "12"]
    db = hin_app[DB]
    docs = db.query("SELECT * FROM documents WHERE source = 'hin'")
    assert {d["practice_id"] for d in docs} == {1}
    assert {d["sender"] for d in docs} == {"ortho@demo-hin.invalid"}
    assert any(d["filename"] == "op.pdf" and d["title"] == "OP-Bericht Muster Lea" for d in docs)
    assert db.value("SELECT COUNT(*) FROM jobs WHERE kind = 'read_document'") >= 2
    # gleiche Mail nochmals → kein Duplikat
    assert await hin_poll_once(hin_app) == 0


def test_parse_ignores_huge_and_keeps_text():
    m = hin.parse("1", _mail("Betreff", ("a.pdf", b"%PDF small")))
    assert m.sender == "ortho@demo-hin.invalid" and m.attachments[0][0] == "a.pdf" and "Anhang" in m.text
