"""Verbindungen prüfen: python -m nfizyo check-connections [--sms NUMMER] [--mail ADRESSE] [--hin ADRESSE] [--praxis KURZNAME] [--ki]

Ohne Zusatz nur lesen und anmelden – es wird nichts gesendet und nichts gebucht:
  KI        KI-Server Schweiz: erreichbar, Token gültig, welche Bausteine bereit (bzw. lokale KI)
  SMS       Zugang beim Anbieter (ASPSMS: Guthaben abfragen)
  E-Mail    SMTP-Server: Verbindung, Verschlüsselung, Anmeldung
  HIN       HIN Client: Senden (SMTP) und Empfangen (IMAP, Posteingang zählen)
  MediData  Box/Webservice erreichbar (mit Client-Zertifikat, falls eingetragen), Ablauf des Client-Zertifikats
            (Hochladen nur über eine Test-Rechnung in N-Fizyo, z. B. an den MediData-Simulator)
  Video     eigener Jitsi-Server: erreichbar, Datenschutz-Einstellungen, keine Gäste
  bexio     Zugang, Konten lesen, Währung CHF

Mit Zusatz wird wirklich gesendet (kostet bei SMS wenige Rappen):
  --sms 0790000000      eine Test-SMS an diese Nummer
  --mail du@beispiel.ch eine Test-E-Mail
  --hin praxis@hin.ch   eine Test-Nachricht über HIN (z. B. an die eigene HIN-Adresse)
  --ki                  kleine echte KI-Aufgaben ohne Patientendaten (1 s Stille erkennen, «Guten Tag» übersetzen)

Zugangsdaten stehen nur in der .env – die Ausgabe zeigt sie nie.
"""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass

OK, FAIL, OFF, WARN = "ok", "fehler", "aus", "hinweis"
SYMBOL = {OK: "✓", FAIL: "✗", OFF: "–", WARN: "!"}


@dataclass
class Check:
    area: str
    status: str
    detail: str


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:160]}"


# --------------------------------------------------------------------------- SMS
async def check_sms(settings, to: str | None) -> list[Check]:
    from .core import sms

    out = []
    if settings.sms_engine != "live":
        out.append(Check("SMS", OFF, "NFIZYO_SMS ist nicht «live» – SMS werden nur protokolliert"))
    providers = {"aspsms": sms.AspSms(settings.aspsms_userkey, settings.aspsms_password),
                 "ecall": sms.ECall(settings.ecall_username, settings.ecall_password)}
    configured = [n for n, p in providers.items() if p.configured]
    if not configured:
        out.append(Check("SMS", OFF, "kein Anbieter eingetragen (NFIZYO_ASPSMS_* oder NFIZYO_ECALL_*)"))
        return out
    if "aspsms" in configured:
        try:
            status, data = await sms.http_post_json("https://json.aspsms.com/CheckCredits",
                                                    {"UserName": settings.aspsms_userkey, "Password": settings.aspsms_password})
            if status == 200 and str(data.get("StatusCode")) == "1":
                out.append(Check("SMS ASPSMS", OK, f"Zugang gültig, Guthaben {data.get('Credits', '?')}"))
            else:
                out.append(Check("SMS ASPSMS", FAIL, f"Antwort {status}: {data.get('StatusCode')} {data.get('StatusInfo') or ''}".strip()))
        except Exception as e:  # noqa: BLE001
            out.append(Check("SMS ASPSMS", FAIL, _err(e)))
    if "ecall" in configured:
        out.append(Check("SMS eCall", WARN, "Zugang eingetragen – prüfen nur mit --sms (echte Test-SMS)"))
    if to:
        number = sms.normalize_phone(to)
        if not number:
            out.append(Check("SMS senden", FAIL, "ungültige Nummer"))
        else:
            name = configured[0]
            res = await providers[name].send(sms.clean_sender("N-Fizyo"), number, "N-Fizyo: Test-SMS. Bitte ignorieren.")
            out.append(Check(f"SMS senden ({name})", OK if res.ok else FAIL, "gesendet" if res.ok else (res.error or "Fehler")))
    return out


# --------------------------------------------------------------------------- E-Mail
def _smtp_login(host: str, port: int, tls: str, user: str, password: str) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    server = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) if tls == "ssl" else smtplib.SMTP(host, port, timeout=20)
    server.ehlo()
    if tls == "starttls":
        server.starttls(context=ctx)
        server.ehlo()
    if user:
        server.login(user, password)
    return server


def check_mail(settings, to: str | None) -> list[Check]:
    from email.message import EmailMessage

    from .core.mail import valid_email

    if not (settings.smtp_host and settings.smtp_from):
        return [Check("E-Mail", OFF, "kein SMTP-Server eingetragen (NFIZYO_SMTP_*)")]
    out = []
    if settings.mail_engine != "live":
        out.append(Check("E-Mail", WARN, "NFIZYO_MAIL ist nicht «live» – E-Mails werden nur protokolliert"))
    try:
        server = _smtp_login(settings.smtp_host, settings.smtp_port, settings.smtp_tls, settings.smtp_user, settings.smtp_password)
        try:
            out.append(Check("E-Mail SMTP", OK, f"{settings.smtp_host}:{settings.smtp_port} ({settings.smtp_tls}), Anmeldung in Ordnung"))
            if to:
                if not valid_email(to):
                    out.append(Check("E-Mail senden", FAIL, "ungültige Adresse"))
                else:
                    msg = EmailMessage()
                    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, "N-Fizyo: Test-E-Mail"
                    msg.set_content("Diese E-Mail prüft den Versand von N-Fizyo. Bitte ignorieren.")
                    server.send_message(msg)
                    out.append(Check("E-Mail senden", OK, "gesendet"))
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        out.append(Check("E-Mail SMTP", FAIL, _err(e)))
    return out


# --------------------------------------------------------------------------- HIN
def check_hin(settings, to: str | None) -> list[Check]:
    from .core.hin import HinError, HinMailbox

    box = HinMailbox(settings)
    if not box.configured:
        return [Check("HIN", OFF, "nicht eingerichtet (NFIZYO_HIN_FROM, NFIZYO_HIN_PRACTICE)")]
    out = []
    try:
        server = _smtp_login(settings.hin_smtp_host, settings.hin_smtp_port, settings.hin_tls,
                             settings.hin_user if settings.hin_tls != "none" else "", settings.hin_password)
        server.quit()
        out.append(Check("HIN senden (SMTP)", OK, f"HIN Client erreichbar ({settings.hin_smtp_host}:{settings.hin_smtp_port})"))
    except Exception as e:  # noqa: BLE001
        out.append(Check("HIN senden (SMTP)", FAIL, _err(e)))
    if box.can_receive:
        try:
            conn = box._imap()
            try:
                typ, data = conn.select("INBOX", readonly=True)
                out.append(Check("HIN empfangen (IMAP)", OK if typ == "OK" else FAIL, f"Posteingang: {data[0].decode() if data else '?'} Nachrichten"))
            finally:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            out.append(Check("HIN empfangen (IMAP)", FAIL, _err(e)))
    else:
        out.append(Check("HIN empfangen (IMAP)", OFF, "kein IMAP-Benutzer (NFIZYO_HIN_USER) – Berichte kommen nicht automatisch"))
    if to:
        try:
            ref = box.send(to, "N-Fizyo: Test über HIN", "Diese Nachricht prüft den Versand über HIN. Bitte ignorieren.", [])
            out.append(Check("HIN senden", OK, f"gesendet ({ref})"))
        except (HinError, OSError, smtplib.SMTPException) as e:
            out.append(Check("HIN senden", FAIL, _err(e)))
    return out


# --------------------------------------------------------------------------- MediData
def check_medidata_cert(settings) -> list[Check]:
    """Client-Zertifikat: gültig bis …; Hinweis ab 60 Tage vor Ablauf; Fehler wenn abgelaufen, Schlüssel passt nicht,
    Schlüsseldatei für andere lesbar."""
    from .modules.billing import medidata_cert

    st = medidata_cert.status(settings)
    if st.state == "off":
        return [] if settings.medidata_mode == "off" else [Check("MediData Zertifikat", OFF, st.message)]
    level = {"ok": OK, "warn": WARN}.get(st.state, FAIL)
    out = [Check("MediData Zertifikat", level, st.message)]
    if st.state in ("ok", "warn"):
        try:
            medidata_cert.endpoint(settings)
        except medidata_cert.CertError as e:
            out.append(Check("MediData Erneuerung", WARN, f"{e} – ohne sie geht «medidata-renew» nicht"))
    return out


async def check_medidata(settings, db=None) -> list[Check]:
    """Zugang mit einem dokumentierten, rein lesenden Aufruf prüfen: GET …/ela/participants?limit=1
    (Schnittstellendokumentation V5.2.0, 3.6) – mit Client-Zertifikat, Basic-Anmeldung und X-CLIENT-ID."""
    from urllib.parse import urlsplit

    from .modules.billing import medidata, medidata_cert
    from .modules.billing.medidata import MdError, MediDataClient

    md = MediDataClient(settings)
    cert = check_medidata_cert(settings)
    if md.mode == "off":
        return [Check("MediData", OFF, "NFIZYO_MEDIDATA=off – nur XML erzeugen")] + cert
    if not md.configured:
        need = "URL, Client-ID, Benutzer, Passwort" + (", Client-Zertifikat (Webservice)" if md.webservice else "")
        return [Check("MediData", FAIL, f"Modus {md.mode}, aber Zugang unvollständig ({need})")] + cert
    try:
        ctx = medidata_cert.client_ssl_context(settings)
    except medidata_cert.CertError as e:
        shown = any(c.status == FAIL for c in cert)            # Grund steht schon in der Zeile «MediData Zertifikat»
        return [Check("MediData", FAIL, "Client-Zertifikat unbrauchbar – siehe «MediData Zertifikat»" if shown
                      else f"Client-Zertifikat: {e}")] + cert
    tls = " mit Client-Zertifikat" if settings.medidata_cert else ""
    host = urlsplit(md.base).hostname or ""
    where = ("Webservice PROD" if host == "services.medidata.ch" else "Webservice ACC (Test)" if host == "services-acc.medidata.ch"
             else "Webservice" if md.webservice else "Box/VA")
    out = []
    if not medidata.client_id_ok(settings):           # Anhang BestPractice: 10 Ziffern Master _ 10 Ziffern Subclient
        out.append(Check("MediData X-CLIENT-ID", WARN, "Format passt nicht zum Webservice (erwartet 1000xxxxxx_1200yyyyyy)"))
    if md.mode == "production" and host == "services-acc.medidata.ch":
        out.append(Check("MediData Umgebung", WARN, "NFIZYO_MEDIDATA=production, aber Adresse der Testumgebung (services-acc)"))
    try:
        async with md.session() as s:
            reply = await md.call(s, "GET", f"{md.base}/ela/participants?limit=1", ctx=ctx)   # nur lesen, nichts senden
        if reply.status == 200 and isinstance(reply.json(), list):
            out.insert(0, Check("MediData", OK, f"Zugang gültig{tls} ({where}, Teilnehmerverzeichnis lesbar), Modus {md.mode}. "
                                                "Test: Leitung › Einstellungen › Zahlung › MediData › Simulator"))
        else:
            hint = " – Pfad prüfen: NFIZYO_MEDIDATA_URL endet vor «/ela» (…/md/srv/id<N>)" if reply.status == 404 else ""
            out.insert(0, Check("MediData", FAIL, f"unerwartete Antwort HTTP {reply.status} auf …/ela/participants ({where}){hint}"))
    except MdError as e:
        hint = ""
        if e.status in (401, 403):
            hint = " – Benutzer/Passwort (Kundenportal › Credentials), X-CLIENT-ID oder Client-Zertifikat prüfen"
        elif e.status is None and settings.medidata_url.startswith("https://"):
            hint = " – Adresse, Serverzertifikat (NFIZYO_MEDIDATA_CA) oder Client-Zertifikat prüfen"
        out.insert(0, Check("MediData", FAIL, f"{e} ({where}){hint}"))
    if db is not None:
        from .modules.billing import medidata_sync

        pid = medidata_sync.owner_practice_id(db, settings)
        if pid is None:
            out.append(Check("MediData Praxis", FAIL, "NFIZYO_MEDIDATA_PRACTICE fehlt oder unbekannt (mehrere Praxen auf der Box)"))
        else:
            from .core.tenant import practice_scope

            with practice_scope(pid):
                errors = {k: v["last_error"] for k, v in medidata_sync.state(db).items() if v.get("last_error")}
            out.append(Check("MediData Abfragen", WARN if errors else OK,
                             "; ".join(f"{k}: {v}" for k, v in errors.items())[:200] if errors else
                             "Verzeichnis 24 h, Status/Antworten/Benachrichtigungen 30 min"))
    return out + cert


# --------------------------------------------------------------------------- Video
async def check_video(app) -> list[Check]:
    from .core.keys import SETTINGS
    from .modules.video.views import check_server

    if not app[SETTINGS].video_url:
        return [Check("Video", OFF, "kein Video-Server eingetragen (NFIZYO_VIDEO_URL)")]
    labels = {"configured": "eingerichtet", "https": "https", "secret_strong": "Schlüssel lang genug", "reachable": "erreichbar",
              "privacy_config": "Datenschutz-Einstellungen aktiv", "no_guests": "keine Gäste"}
    return [Check(f"Video: {labels.get(k, k)}", OK if ok else FAIL, "") for k, ok in await check_server(app)]


# --------------------------------------------------------------------------- bexio
async def check_bexio(app, practice_slug: str | None) -> list[Check]:
    from .core.keys import DB, SETTINGS
    from .core.tenant import practice_scope
    from .modules.exports import bexio

    settings = app[SETTINGS]
    mode = bexio.mode(settings)
    if mode == "off":
        return [Check("bexio", OFF, "nicht eingerichtet (NFIZYO_BEXIO_TOKEN oder _CLIENT_ID/_SECRET)")]
    db = app[DB]
    if practice_slug:
        row = db.one("SELECT id FROM practices WHERE slug = ?", (practice_slug,))
    elif mode == "token":                      # Token gehört einer Praxis (NFIZYO_BEXIO_PRACTICE, sonst die einzige der Box)
        pid = bexio.token_practice_id(db, settings)
        if pid is None and not db.value("SELECT COUNT(*) FROM practices"):
            try:                               # noch keine Praxis: nur das Token selbst prüfen
                res = await bexio.check(db, settings, set(), token=settings.bexio_token)
                return [Check("bexio", OK, f"Zugang gültig (Token), {res['accounts']} Konten, CHF vorhanden – noch keine Praxis")]
            except (bexio.BexioError, bexio.Unclear) as e:
                return [Check("bexio", FAIL, str(e))]
        if pid is None:
            return [Check("bexio", FAIL, "NFIZYO_BEXIO_PRACTICE fehlt oder unbekannt (mehrere Praxen auf der Box) – Token für niemanden aktiv")]
        row = {"id": pid}
    else:
        row = db.one("SELECT id FROM practices ORDER BY id LIMIT 1")
    if row is None and (practice_slug or mode == "oauth"):
        return [Check("bexio", FAIL, f"Praxis «{practice_slug}» nicht gefunden" if practice_slug else "noch keine Praxis angelegt")]
    with practice_scope(row["id"] if row else None):
        here = bexio.mode_here(db, settings) if row is not None else mode
        if here not in ("token", "oauth"):
            return [Check("bexio", FAIL, f"Token gehört nicht der Praxis «{practice_slug}» (NFIZYO_BEXIO_PRACTICE)")]
        if here == "oauth" and not bexio.connected(db, settings):
            return [Check("bexio", WARN, "OAuth eingerichtet – die Leitung muss noch «Mit bexio verbinden» klicken")]
        try:
            res = await bexio.check(db, settings, set())
            return [Check("bexio", OK, f"Zugang gültig ({'Token' if here == 'token' else 'OAuth'}), {res['accounts']} Konten, CHF vorhanden")]
        except (bexio.BexioError, bexio.Unclear) as e:
            return [Check("bexio", FAIL, str(e))]


# --------------------------------------------------------------------------- KI
PARTS = (("stt", "Spracherkennung"), ("llm", "Sprachmodell"), ("translate", "Übersetzung"))


def check_ai(app, test: bool = False) -> list[Check]:
    """Läuft in einem Thread (fragt den KI-Server direkt, ohne Zwischenspeicher)."""
    import tempfile
    import time
    from pathlib import Path

    from .core.keys import AI

    ai = app[AI]
    if ai.mode == "local":
        st = ai.status()
        out = [Check(f"KI lokal: {label}", OK if st[k]["available"] else OFF, "" if st[k]["available"] else "nicht eingerichtet")
               for k, label in PARTS]
    else:
        client = ai.remote or ai.cloud
        t0 = time.monotonic()
        raw = client.status(max_age=0)
        ms = int((time.monotonic() - t0) * 1000)
        st = ai.status()
        name = "KI-Server" if ai.mode == "remote" else "KI (Vorführung)"
        if st.get("error"):
            return [Check(name, FAIL, f"{st['error']} ({st['server']})")]
        out = [Check(name, OK if ai.mode == "remote" else WARN,
                     f"{st['server']} erreichbar, Zugang gültig, Antwort in {ms} ms"
                     + ("" if ai.mode == "remote" else " – nur fiktive Daten (cloud_demo)"))]
        out += [Check(f"{name}: {label}", OK if st[k]["available"] else FAIL, "" if st[k]["available"] else "dort nicht bereit")
                for k, label in PARTS]
        if isinstance(raw.get("max_audio"), int):
            out.append(Check(f"{name}: Aufnahmen", OK, f"bis {raw['max_audio'] // (1024 * 1024)} MB pro Diktat"))
    if not test:
        return out
    from .ai.cloud import silent_wav

    if ai.can_transcribe():
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "stille.wav"
            wav.write_bytes(silent_wav())
            t0 = time.monotonic()
            try:
                ai.transcribe(wav, "de")
                out.append(Check("KI-Test: Spracherkennung", OK, f"1 s Stille erkannt in {int((time.monotonic() - t0) * 1000)} ms"))
            except Exception as e:  # noqa: BLE001 - Ergebnis der Prüfung
                out.append(Check("KI-Test: Spracherkennung", FAIL, _err(e)))
    if ai.can_translate():
        t0 = time.monotonic()
        res = ai.translator.translate("Guten Tag", "de", "fr")
        out.append(Check("KI-Test: Übersetzung", OK if res.text else FAIL,
                         f"«Guten Tag» → «{(res.text or '–')[:40]}» in {int((time.monotonic() - t0) * 1000)} ms"))
    return out


# --------------------------------------------------------------------------- Ablauf
def check_base(settings) -> list[Check]:
    out = []
    if settings.base_url.startswith("https://"):
        out.append(Check("Adresse", OK, settings.base_url))
    else:
        out.append(Check("Adresse", WARN, f"{settings.base_url} – im Betrieb muss es https sein (Links in SMS, Video, bexio)"))
    if settings.demo:
        out.append(Check("Demo", WARN, "NFIZYO_DEMO=1 – Demo-Daten, nicht für echte Patienten"))
    return out


async def run_all(app, *, sms_to=None, mail_to=None, hin_to=None, practice=None, ai_test=False) -> list[Check]:
    from .core.keys import DB, SETTINGS

    s = app[SETTINGS]
    checks = check_base(s)
    checks += await asyncio.to_thread(check_ai, app, ai_test)
    checks += await check_sms(s, sms_to)
    checks += await asyncio.to_thread(check_mail, s, mail_to)
    checks += await asyncio.to_thread(check_hin, s, hin_to)
    checks += await check_medidata(s, app[DB])
    checks += await check_video(app)
    checks += await check_bexio(app, practice)
    return checks


def render(checks: list[Check]) -> str:
    width = max(len(c.area) for c in checks) if checks else 10
    lines = [f" {SYMBOL[c.status]}  {c.area.ljust(width)}  {c.detail}".rstrip() for c in checks]
    fails = sum(1 for c in checks if c.status == FAIL)
    lines.append("")
    lines.append("Alles in Ordnung." if not fails else f"{fails} Fehler – Details oben. Anleitung: docs/VERBINDUNGSTEST.md")
    return "\n".join(lines)
