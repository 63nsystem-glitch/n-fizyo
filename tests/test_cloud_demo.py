"""NUR Vorführung: KI über einen OpenAI-kompatiblen Cloud-Dienst (NFIZYO_AI_MODE=cloud_demo).

Geprüft mit einem lokalen Nachbau des Dienstes: Diktat → Text → Entwurf → Patienteninformation → Übersetzung in der
Demo-Praxis; Schlüssel nie in Logs, Seiten oder check-ai; ohne Demo-Modus startet nichts; Sicherheitsprüfungen bleiben;
antwortet der Dienst zu langsam, bleibt das Diktat gespeichert und wird später verarbeitet.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import csrf_of, login, pid_of

from nfizyo.ai.cloud import CloudAIClient, CloudDemoRefused, CloudLLM, CloudSTT, CloudTranslator
from nfizyo.ai.service import AIServices
from nfizyo.app import create_app
from nfizyo.core.config import load_settings
from nfizyo.core.db import utcnow
from nfizyo.core.keys import DB, JOBS

KEY = "sk-demo-test-0123456789-GEHEIM-abcd"
TRANSCRIPT_DE = ("Knie links Flexion 95 Grad, VAS 3. Mobilisation und Kräftigung durchgeführt. "
                 "Zu Hause die Übungen zweimal täglich machen. Sie soll sich melden, wenn das Bein taub wird.")
TRANSCRIPT_FR = "Genou gauche flexion 95 degrés. Mobilisation réalisée. À la maison, faire les exercices chaque jour."
NOTE = {"befund": "Knie links: Flexion 95°, VAS 3/10.", "behandlung": "Mobilisation und Kräftigung.", "beurteilung": "",
        "plan": "Heimprogramm zweimal täglich."}
INFO = {"home": "Machen Sie die Übungen zweimal täglich.", "contact": "Melden Sie sich, wenn das Bein taub wird."}


class FakeOpenAI:
    """Kleiner OpenAI-kompatibler Dienst (eigener Thread, damit die Box ihn wie einen echten Dienst anfragt)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.transcripts = {"de": TRANSCRIPT_DE, "fr": TRANSCRIPT_FR}
        self.note = dict(NOTE)
        self.stt_delay = 0.0
        self.reject_temperature = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # still
                pass

            def _send(self, status, data):
                body = json.dumps(data).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):  # Box hat nach dem Zeitlimit aufgegeben
                    pass

            def _auth(self) -> bool:
                got = self.headers.get("Authorization", "")
                if got != f"Bearer {KEY}":
                    # wie echte Dienste: der (falsche) Schlüssel steht in der Fehlermeldung
                    self._send(401, {"error": {"message": f"Incorrect API key provided: {got[7:]}"}})
                    return False
                return True

            def do_GET(self):
                fake.calls.append({"path": self.path})
                if self._auth():
                    self._send(200, {"data": [{"id": "fake-transcribe"}, {"id": "fake-chat", "aliases": ["fake-latest"]}]})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                call = {"path": self.path, "content_type": self.headers.get("Content-Type", "")}
                fake.calls.append(call)
                if not self._auth():
                    return
                if self.path == "/v1/audio/transcriptions":
                    msg = BytesParser(policy=policy.default).parsebytes(
                        b"Content-Type: " + call["content_type"].encode() + b"\r\n\r\n" + body)
                    fields = {}
                    for part in msg.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        fields[name] = part.get_filename() or part.get_content()
                    call["fields"] = fields
                    if fake.stt_delay:
                        time.sleep(fake.stt_delay)
                    lang = fields.get("language") or fields.get("languages[]") or "de"
                    self._send(200, {"text": fake.transcripts.get(lang, "") if fields.get("file") != "audio.wav" else ""})
                    return
                if self.path == "/v1/chat/completions":
                    data = json.loads(body)
                    call["json"] = data
                    if fake.reject_temperature and "temperature" in data:
                        self._send(400, {"error": {"message": "Unsupported parameter: 'temperature' is not supported with this model."}})
                        return
                    system = next((m["content"] for m in data["messages"] if m["role"] == "system"), "")
                    user = data["messages"][-1]["content"]
                    if "Dokumentationshilfe" in system or "documenter" in system:
                        out = json.dumps(fake.note)
                    elif "Patientinnen und Patienten einer Physiotherapie" in system or "patient·e·s" in system:
                        out = "```json\n" + json.dumps(INFO) + "\n```"  # manche Modelle setzen Code-Zäune
                    elif system:
                        out = json.dumps({"items": []})
                    elif user.startswith("You are a professional"):
                        out = "[tr] " + user.rsplit("\n\n\n", 1)[-1]
                    else:
                        out = "OK"
                    self._send(200, {"choices": [{"message": {"role": "assistant", "content": out}}]})
                    return
                self._send(404, {"error": {"message": "not found"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def paths(self, path: str) -> list[dict]:
        return [c for c in self.calls if c["path"] == path]


@pytest.fixture
def fake():
    f = FakeOpenAI()
    f.thread.start()
    yield f
    f.server.shutdown()
    f.server.server_close()


def _settings(tmp_path, fake, **kw):
    base = dict(data_dir=tmp_path, secret_key=b"c" * 32, demo=True, ai_mode="cloud_demo", cloud_ai_url=fake.url + "/v1",
                cloud_ai_key=KEY, cloud_ai_stt_model="fake-transcribe", cloud_ai_llm_model="fake-chat",
                cloud_ai_translate_model="", cloud_ai_timeout=3.0)
    base.update(kw)
    return load_settings(None, **base)


@pytest.fixture
def cloud_app(tmp_path, fake):
    return create_app(_settings(tmp_path, fake), start_workers=False)


async def _upload(client, csrf, patient_id, appointment_id, lang="de"):
    from aiohttp import FormData

    fd = FormData()
    fd.add_field("client_uuid", uuid.uuid4().hex)
    fd.add_field("patient_id", str(patient_id))
    fd.add_field("appointment_id", str(appointment_id or ""))
    fd.add_field("lang", lang)
    fd.add_field("audio", b"\x1aE\xdf\xa3fiktive-demo-aufnahme", filename="a.webm", content_type="audio/webm")
    r = await client.post("/therapie/diktat/upload", data=fd, headers={"X-CSRF-Token": csrf})
    assert r.status == 200
    return (await r.json())["id"]


# --------------------------------------------------------------------------- Ende zu Ende in der Demo-Praxis
async def test_dictation_draft_info_translation_end_to_end(aiohttp_client, cloud_app, fake, caplog):
    caplog.set_level(logging.DEBUG)
    app, db = cloud_app, cloud_app[DB]
    client = await aiohttp_client(app)
    ayse = pid_of(app, "Ayşe")
    aid = db.value("SELECT id FROM appointments WHERE patient_id = ? AND status IN ('booked','arrived') ORDER BY starts_at", (ayse,))
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get(f"/therapie/diktat/termin/{aid}")).text()
    assert "simuliert" not in page and "Demo: KI über externen Dienst 127.0.0.1" in page and KEY not in page

    rid = await _upload(client, csrf, ayse, aid)
    await app[JOBS].run_all(app)
    rec = db.one("SELECT * FROM recordings WHERE id = ?", (rid,))
    assert rec["status"] == "drafted" and rec["transcript"] == TRANSCRIPT_DE and rec["transcript_engine"] == "cloud:fake-transcribe"
    stt = fake.paths("/v1/audio/transcriptions")[0]
    assert stt["fields"]["model"] == "fake-transcribe" and stt["fields"]["language"] == "de" and stt["fields"]["file"] == "audio.webm"
    draft = json.loads(rec["draft_json"])
    assert draft["engine"] == "llm:fake-chat" and draft["befund"] == NOTE["befund"] and not draft["warnings"]
    assert any(m["kind"] == "flexion" and m["value"] == 95 for m in draft["measurements"])  # Messwerte aus dem Diktat (Regeln)
    chat = [c["json"] for c in fake.paths("/v1/chat/completions")]
    assert all(c["model"] == "fake-chat" for c in chat)
    # Je nach Tageszeit übersetzt die Heute-Seite vorher ein Patientenziel – den Entwurfsaufruf gezielt suchen.
    drafts = [c for c in chat if "Dokumentationshilfe" in c["messages"][0]["content"]]   # gleiche Prompts wie lokal
    assert drafts and drafts[0]["response_format"] == {"type": "json_object"}

    review = f"/therapie/diktat/{rid}"
    page = await (await client.get(review)).text()
    assert "cloud:fake-transcribe" in page and "simuliert" not in page and KEY not in page
    info = db.one("SELECT * FROM patient_infos WHERE recording_id = ?", (rid,))
    assert info["home"] == INFO["home"] and info["contact"] == INFO["contact"] and info["ai_engine"] == "llm:fake-chat"

    form = {"_csrf": csrf_of(page), "pi_home": INFO["home"], "pi_contact": INFO["contact"], "pi_send": "1"}
    for sec in ("befund", "behandlung", "beurteilung", "plan"):
        m = re.search(rf'name="{sec}"[^>]*>([^<]*)<', page)
        form[sec] = m.group(1) if m else ""
    r = await client.post(review + "/freigeben", data=form, allow_redirects=False)
    assert r.status == 302
    await app[JOBS].run_all(app)
    info = db.one("SELECT * FROM patient_infos WHERE recording_id = ?", (rid,))
    assert info["status"] == "approved" and info["target_lang"] == "tr"
    assert info["home_translated"] == "[tr] " + INFO["home"] and info["translation_engine"] == "cloud:fake-chat"

    # Französisches Diktat: Sprachhinweis fr
    lea = pid_of(app, "Lea")
    await _upload(client, csrf, lea, None, lang="fr")
    await app[JOBS].run_all(app)
    assert fake.paths("/v1/audio/transcriptions")[-1]["fields"]["language"] == "fr"
    assert db.value("SELECT transcript FROM recordings WHERE patient_id = ? ORDER BY id DESC", (lea,)) == TRANSCRIPT_FR

    # Einstellungen › KI: Hinweis, Anbieter und Modelle sichtbar, Schlüssel nie
    await login(client, "leitung@demo.n-fizyo.ch")
    page = await (await client.get("/leitung/einstellungen/ki")).text()
    assert "Demo: KI über externen Dienst 127.0.0.1 – nur für Vorführungen mit fiktiven Daten" in page
    assert "fake-transcribe" in page and "fake-chat" in page and KEY not in page and KEY[-8:] not in page
    assert KEY not in caplog.text
    assert not db.value("SELECT COUNT(*) FROM jobs WHERE error LIKE ?", (f"%{KEY[3:12]}%",))


async def test_french_settings_page(aiohttp_client, cloud_app):
    db = cloud_app[DB]
    db.execute("UPDATE users SET lang = 'fr' WHERE email = 'leitung@demo.n-fizyo.ch'")
    client = await aiohttp_client(cloud_app)
    await login(client, "leitung@demo.n-fizyo.ch")
    page = await (await client.get("/leitung/einstellungen/ki")).text()
    assert "Démo : IA via un service externe 127.0.0.1 – uniquement pour des présentations avec des données fictives" in page


# --------------------------------------------------------------------------- Sicherheitsprüfungen bleiben
def test_invented_number_rejects_draft(tmp_path, fake):
    fake.note = {**NOTE, "befund": "Knie links: Flexion 120°."}  # 120 steht nicht im Diktat
    ai = AIServices(_settings(tmp_path, fake))
    draft = ai.draft_note(TRANSCRIPT_DE, "de")
    assert "note.llm_numbers_rejected" in draft.warnings and draft.engine == "rules" and "120" not in draft.befund
    fake.note = "kein JSON"
    assert "note.llm_invalid" in ai.draft_note(TRANSCRIPT_DE, "de").warnings


def test_report_quotes_must_exist_and_translation_numbers_checked(tmp_path, fake):
    ai = AIServices(_settings(tmp_path, fake))
    llm = ai.llm
    llm.generate = lambda *a, **k: json.dumps({"items": [{"kind": "restriction", "text": "Nicht joggen", "excerpt": "Joggen verboten"}]})
    result = ai.extract("Diagnose: Gonarthrose links. Kontrolle in 6 Wochen.", "de")
    assert "extract.llm_dropped" in result.warnings and all("Joggen" not in s.excerpt for s in result.suggestions)
    tr = CloudTranslator(CloudLLM(ai.cloud, "fake-chat"))
    tr.llm.generate = lambda *a, **k: "Günde 3 kez"
    assert tr.translate("Zweimal täglich, 2 Sätze", "de", "tr").warning == "translate.numbers_differ"


def test_unsupported_temperature_is_dropped(tmp_path, fake):
    fake.reject_temperature = True
    client = CloudAIClient(fake.url + "/v1", KEY, demo=True, timeout=3)
    assert client.chat("fake-chat", "", "Antworte nur mit: OK") == "OK"
    assert client.chat("fake-chat", "", "Antworte nur mit: OK") == "OK"
    sent = [c["json"] for c in fake.paths("/v1/chat/completions")]
    assert "temperature" in sent[0] and "temperature" not in sent[1] and "temperature" not in sent[2]  # gemerkt


# --------------------------------------------------------------------------- Nur im Demo-Modus
def test_refuses_without_demo_mode(tmp_path, fake):
    s = _settings(tmp_path, fake, demo=False, require_2fa=False)
    with pytest.raises(CloudDemoRefused, match="NFIZYO_DEMO=1"):
        create_app(s, start_workers=False, seed_demo=False)
    assert not (tmp_path / "n-fizyo.sqlite3").exists()  # nichts angelegt, nichts gestartet
    with pytest.raises(CloudDemoRefused):
        AIServices(s)
    with pytest.raises(CloudDemoRefused):
        CloudAIClient(fake.url + "/v1", KEY, demo=False)
    assert not fake.calls  # nichts wurde gesendet


def test_cli_refuses_to_start_without_demo(tmp_path, fake, monkeypatch, capsys):
    from nfizyo.__main__ import main

    env = tmp_path / "n-fizyo.env"
    env.write_text(f"NFIZYO_SECRET_KEY=x\nNFIZYO_DATA_DIR={tmp_path / 'data'}\nNFIZYO_DEMO=0\nNFIZYO_AI_MODE=cloud_demo\n"
                   f"NFIZYO_CLOUD_AI_URL={fake.url}/v1\nNFIZYO_CLOUD_AI_KEY={KEY}\nNFIZYO_CLOUD_AI_STT_MODEL=fake-transcribe\n"
                   "NFIZYO_CLOUD_AI_LLM_MODEL=fake-chat\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["--env", str(env), "run"])
    assert "N-Fizyo startet nicht" in str(exc.value.code) and "nur im Demo-Modus" in str(exc.value.code)
    assert KEY not in str(exc.value.code) and not fake.calls


def test_refuses_with_real_practice_in_database(tmp_path, fake):
    app = create_app(load_settings(None, data_dir=tmp_path, secret_key=b"c" * 32, demo=True), start_workers=False, seed_demo=False)
    app[DB].execute("INSERT INTO practices (name, slug, default_lang, created_at) VALUES ('Echt', 'echt', 'de', ?)", (utcnow(),))
    app[DB].close()
    with pytest.raises(CloudDemoRefused, match="keine Demo-Praxis"):
        create_app(_settings(tmp_path, fake), start_workers=False, seed_demo=False)


def test_only_https_and_required_settings(tmp_path, fake):
    with pytest.raises(CloudDemoRefused, match="HTTPS"):
        CloudAIClient("http://api.example.com/v1", KEY, demo=True)
    with pytest.raises(CloudDemoRefused, match="NFIZYO_CLOUD_AI_KEY"):
        CloudAIClient("https://api.example.com/v1", "", demo=True)
    with pytest.raises(CloudDemoRefused, match="NFIZYO_CLOUD_AI_STT_MODEL"):
        AIServices(_settings(tmp_path, fake, cloud_ai_stt_model=""))
    s = _settings(tmp_path, fake)
    assert KEY not in repr(s) and KEY not in repr(CloudAIClient(fake.url + "/v1", KEY, demo=True))


# --------------------------------------------------------------------------- check-ai
def _env(tmp_path, fake, key=KEY):
    env = tmp_path / "demo.env"
    env.write_text(f"NFIZYO_SECRET_KEY=x\nNFIZYO_DATA_DIR={tmp_path / 'data'}\nNFIZYO_DEMO=1\nNFIZYO_AI_MODE=cloud_demo\n"
                   f"NFIZYO_CLOUD_AI_URL={fake.url}/v1\nNFIZYO_CLOUD_AI_KEY={key}\nNFIZYO_CLOUD_AI_STT_MODEL=fake-transcribe\n"
                   "NFIZYO_CLOUD_AI_LLM_MODEL=fake-chat\n", encoding="utf-8")
    return env


def test_check_ai_tests_provider_and_masks_key(tmp_path, fake, capsys, caplog):
    from nfizyo.__main__ import main

    main(["--env", str(_env(tmp_path, fake)), "check-ai"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["mode"] == "cloud_demo" and data["stt"]["model"] == "fake-transcribe" and data["translate"]["model"] == "fake-chat"
    test = data["cloud_test"]
    assert test["llm"]["ok"] and test["stt"]["ok"] and test["translate"]["ok"] and test["only_fictitious_data"]
    assert test["key"] == f"gesetzt (…{KEY[-4:]})"
    assert KEY not in out and KEY not in caplog.text
    stt = fake.paths("/v1/audio/transcriptions")[0]["fields"]
    assert stt["file"] == "audio.wav" and stt["model"] == "fake-transcribe"     # Stille, keine Patientendaten


def test_check_ai_wrong_key_is_not_echoed(tmp_path, fake, capsys, caplog):
    from nfizyo.__main__ import main

    wrong = "sk-falsch-9876543210-ZZZZ-yyyy"
    main(["--env", str(_env(tmp_path, fake, key=wrong)), "check-ai"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert not data["llm"]["available"] and not data["cloud_test"]["llm"]["ok"]
    assert "NFIZYO_CLOUD_AI_KEY prüfen" in data["cloud_test"]["llm"]["error"]
    assert wrong not in out and wrong not in caplog.text


# --------------------------------------------------------------------------- Dienst zu langsam → Diktat wartet
async def test_timeout_keeps_dictation_for_later(aiohttp_client, tmp_path, fake):
    from nfizyo.modules.dictation.views import retry_waiting

    app = create_app(_settings(tmp_path, fake, cloud_ai_timeout=0.5), start_workers=False)
    db = app[DB]
    client = await aiohttp_client(app)
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    fake.stt_delay = 1.5
    rid = await _upload(client, csrf, pid_of(app, "Lea"), None)
    await app[JOBS].run_all(app)
    rec = db.one("SELECT * FROM recordings WHERE id = ?", (rid,))
    assert rec["status"] == "failed" and rec["error"] == "stt_unavailable" and not rec["transcript"]   # kein Beispieltext
    assert rec["path"] and Path(rec["path"]).exists()                                                 # Aufnahme bleibt
    fake.stt_delay = 0
    assert retry_waiting(app) >= 1
    await app[JOBS].run_all(app)
    rec = db.one("SELECT * FROM recordings WHERE id = ?", (rid,))
    assert rec["status"] == "drafted" and rec["transcript"] == TRANSCRIPT_DE


def test_unreachable_service_is_reported_not_simulated(tmp_path):
    s = load_settings(None, data_dir=tmp_path, secret_key=b"c" * 32, demo=True, ai_mode="cloud_demo",
                      cloud_ai_url="http://127.0.0.1:9/v1", cloud_ai_key=KEY, cloud_ai_stt_model="m", cloud_ai_llm_model="m",
                      cloud_ai_timeout=1.0)
    ai = AIServices(s)
    assert not ai.simulate_demo and not ai.can_transcribe()
    st = ai.status()
    assert not st["stt"]["available"] and "nicht erreichbar" in st["error"]
    draft = ai.draft_note(TRANSCRIPT_DE, "de")
    assert "note.llm_unavailable" in draft.warnings and draft.engine == "rules"   # wie bisher: Regel-Engine, offen angesagt
    assert CloudSTT(ai.cloud, "m").available() is False


# --------------------------------------------------------------------------- QA-Fix SEC-08: Prüfung vor JEDER Anfrage
def test_real_practice_created_while_running_stops_cloud_calls_at_once(tmp_path, fake, caplog):
    """Eine echte Praxis, die im Betrieb entsteht (oder eine Demo-Praxis, die als echt markiert wird), sperrt sofort jede
    weitere Anfrage an den externen Dienst – nicht erst beim nächsten Start."""
    from nfizyo.ai.cloud import CloudAIError
    from nfizyo.ai.llm import LLMUnavailable
    from nfizyo.core.keys import AI

    app = create_app(_settings(tmp_path, fake), start_workers=False)
    ai = app[AI]
    assert ai.cloud.guard is not None
    assert ai.llm.generate("Antworte nur mit: OK") == "OK"             # nur Demo-Praxen → erlaubt
    sent = len(fake.paths("/v1/chat/completions"))
    app[DB].execute("INSERT INTO practices (name, slug, default_lang, created_at) VALUES ('Echte Praxis', 'echt', 'de', ?)", (utcnow(),))
    with caplog.at_level(logging.ERROR):
        with pytest.raises(LLMUnavailable, match="gesperrt"):
            ai.llm.generate("Antworte nur mit: OK")
        with pytest.raises(CloudAIError, match="gesperrt"):
            ai.cloud.transcribe("fake-transcribe", b"x", "a.webm", "de")
    assert len(fake.paths("/v1/chat/completions")) == sent and not fake.paths("/v1/audio/transcriptions")
    assert "gesperrt" in caplog.text and KEY not in caplog.text
    # Demo-Praxis als echt markiert → ebenso gesperrt
    app[DB].execute("DELETE FROM practices WHERE slug = 'echt'")
    assert ai.llm.generate("Antworte nur mit: OK") == "OK"
    app[DB].execute("UPDATE practices SET is_demo = 0 WHERE id = 1")
    with pytest.raises(LLMUnavailable):
        ai.llm.generate("Antworte nur mit: OK")


def test_cli_create_practice_refused_with_cloud_demo(tmp_path, fake):
    from nfizyo.__main__ import main

    env = tmp_path / "n-fizyo.env"
    env.write_text(f"NFIZYO_SECRET_KEY=x\nNFIZYO_DATA_DIR={tmp_path / 'data'}\nNFIZYO_DEMO=1\nNFIZYO_AI_MODE=cloud_demo\n"
                   f"NFIZYO_CLOUD_AI_URL={fake.url}/v1\nNFIZYO_CLOUD_AI_KEY={KEY}\nNFIZYO_CLOUD_AI_STT_MODEL=fake-transcribe\n"
                   "NFIZYO_CLOUD_AI_LLM_MODEL=fake-chat\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["--env", str(env), "create-practice", "--name", "Echte Praxis", "--slug", "echt"])
    assert "cloud_demo" in str(exc.value.code) and KEY not in str(exc.value.code)
    assert not (tmp_path / "data" / "n-fizyo.sqlite3").exists() or not create_app(
        load_settings(str(env), ai_mode="local"), start_workers=False, seed_demo=False)[DB].value(
        "SELECT 1 FROM practices WHERE slug = 'echt'")
