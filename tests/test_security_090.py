"""Sicherheit 0.9.0: echte Adresse hinter dem Proxy, Bremsen (Adresse, Konto, SMS), Zwei-Schritt-Code, Sitzungen
(Inaktivität, deaktiviert, neues Passwort, App-Zugang beendet), Passwort-Hash, SMS-Codes maskiert, Formeln in CSV/Excel,
PDF-Text in eigenem Prozess."""
from __future__ import annotations

import asyncio
import csv
import io
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone
from html import unescape
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp.test_utils import make_mocked_request
from conftest import PASSWORD, csrf_of, login, pid_of

from nfizyo.app import create_app
from nfizyo.core import crypto, security, sms, totp
from nfizyo.core import export as core_export
from nfizyo.core.auth import LoginThrottle, create_session
from nfizyo.core.config import load_settings
from nfizyo.core.db import utcnow
from nfizyo.core.keys import DB, SETTINGS, SMS, THROTTLE
from nfizyo.core.spreadsheet import safe_cell, safe_row
from nfizyo.core.web import client_ip

AYSE = {"phone": "079 000 00 08", "birthdate": "1980-07-14"}


def _iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- echte Adresse (client_ip)
def _req(remote: str, xff: str | None = None, trusted=None):
    transport = mock.Mock()
    transport.get_extra_info = lambda name, default=None: (remote, 40000) if name == "peername" else default
    headers = {"X-Forwarded-For": xff} if xff is not None else {}
    req = make_mocked_request("GET", "/", headers=headers, transport=transport)
    return client_ip(req, trusted)


def test_client_ip_trusts_only_the_proxy():
    local = ("127.0.0.1", "::1")
    assert _req("127.0.0.1", "198.51.100.7", local) == "198.51.100.7"                 # Caddy hängt die echte Adresse an
    assert _req("127.0.0.1", "6.6.6.6, 198.51.100.7", local) == "198.51.100.7"        # links vom Besucher erfunden → egal
    assert _req("::1", "2001:db8::5", local) == "2001:db8::5"
    assert _req("127.0.0.1", None, local) == "127.0.0.1"
    assert _req("127.0.0.1", "kein-ip", local) == "127.0.0.1"                         # kaputter Kopf → nichts glauben
    # direkt verbunden (kein Proxy): ein mitgeschickter Kopf wird ignoriert
    assert _req("203.0.113.9", "1.2.3.4", local) == "203.0.113.9"
    assert _req("127.0.0.1", "1.2.3.4", ()) == "127.0.0.1"                            # NFIZYO_TRUSTED_PROXIES=none
    # mehrere eigene Proxys (Netz) werden übersprungen
    assert _req("10.1.2.3", "198.51.100.7, 10.0.0.5", ("10.0.0.0/8",)) == "198.51.100.7"
    assert _req("::ffff:127.0.0.1", "198.51.100.8", local) == "198.51.100.8"


def test_trusted_proxies_and_idle_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NFIZYO_TRUSTED_PROXIES", "10.0.0.0/8, 127.0.0.1")
    monkeypatch.setenv("NFIZYO_STAFF_IDLE_HOURS", "4")
    s = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32)
    assert s.trusted_proxies == ("10.0.0.0/8", "127.0.0.1") and s.staff_idle_hours == 4.0
    monkeypatch.setenv("NFIZYO_TRUSTED_PROXIES", "none")
    monkeypatch.delenv("NFIZYO_STAFF_IDLE_HOURS")
    s = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32)
    assert s.trusted_proxies == () and s.staff_idle_hours == 8.0


# --------------------------------------------------------------------------- Bremse (im Speicher)
def test_throttle_windows_delay_and_cleanup():
    now = [1000.0]
    t = LoginThrottle(clock=lambda: now[0])
    for _ in range(3):
        t.fail("sms:1", window=3600)
    assert t.blocked("sms:1", 3, 3600) and not t.blocked("sms:1", 4, 3600)
    now[0] += 3601
    assert not t.blocked("sms:1", 3, 3600)
    # Konto: 5 Fehlversuche frei, dann 30 s, 60 s … höchstens 15 Minuten
    for _ in range(5):
        t.fail("acct", window=3600)
    assert t.delay("acct") == 30
    t.fail("acct", window=3600)
    assert t.delay("acct") == 60
    for _ in range(10):
        t.fail("acct", window=3600)
    assert t.delay("acct") == 900
    now[0] += 901
    assert t.delay("acct") == 0
    # alte Einträge verschwinden (sonst wächst der Speicher)
    t.fail("x", window=60)
    now[0] += 3601
    t.cleanup()
    assert t.failures == {}
    for _ in range(500):
        t.fail("viel")
    assert len(t.failures["viel"]) == LoginThrottle.MAX_PER_KEY


# --------------------------------------------------------------------------- Praxis-Anmeldung: Adresse und Konto
async def _post_login(client, email, password, ip=None):
    page = await client.get("/login")
    headers = {"X-Forwarded-For": ip} if ip else {}
    return await client.post("/login", data={"_csrf": csrf_of(await page.text()), "email": email, "password": password},
                             headers=headers, allow_redirects=False)


async def test_login_limit_is_per_real_address(client, app):
    """Hinter Caddy: 30 Fehlversuche von einer Adresse sperren nur diese Adresse – nicht alle Praxen."""
    for i in range(30):
        r = await _post_login(client, f"niemand{i}@example.invalid", "falsch", ip="198.51.100.1")
        assert r.status == 401
    r = await _post_login(client, "empfang@demo.n-fizyo.ch", PASSWORD, ip="198.51.100.1")
    assert r.status == 429
    client.session.cookie_jar.clear()
    r = await _post_login(client, "empfang@demo.n-fizyo.ch", PASSWORD, ip="198.51.100.2")
    assert r.status == 302


async def test_spoofed_header_without_proxy_is_ignored(aiohttp_client, tmp_path):
    app = create_app(load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True, trusted_proxies=()), start_workers=False)
    client = await aiohttp_client(app)
    for i in range(30):   # jede Anfrage mit einer anderen, erfundenen Adresse
        await _post_login(client, f"niemand{i}@example.invalid", "falsch", ip=f"203.0.113.{i}")
    r = await _post_login(client, "empfang@demo.n-fizyo.ch", PASSWORD, ip="203.0.113.200")
    assert r.status == 429


async def test_account_pause_independent_of_address_but_short(client, app):
    now = [time.time()]
    app[THROTTLE]._clock = lambda: now[0]
    email = "leitung@demo.n-fizyo.ch"
    # bekanntes Gerät: hier hat sich die Person schon angemeldet
    r = await _post_login(client, email, PASSWORD, ip="198.51.100.10")
    assert r.status == 302
    device = client.session.cookie_jar.filter_cookies(client.make_url("/"))["nf_dev"].value
    client.session.cookie_jar.clear()
    for i in range(5):   # 5 Fehlversuche von 5 verschiedenen Adressen
        assert (await _post_login(client, email, "falsch", ip=f"203.0.113.{i}")).status == 401
    r = await _post_login(client, email, PASSWORD, ip="203.0.113.99")
    assert r.status == 429 and "Zu viele Versuche" in await r.text()
    # auf dem bekannten Gerät gilt die Konto-Pause nicht (niemand kann ein Konto von aussen aussperren)
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({"nf_dev": device})
    assert (await _post_login(client, email, PASSWORD, ip="198.51.100.10")).status == 302
    # sonst: kurze Pause (30 s), danach geht es wieder
    client.session.cookie_jar.clear()
    for i in range(5):
        await _post_login(client, email, "falsch", ip=f"203.0.113.{i + 10}")
    assert (await _post_login(client, email, PASSWORD, ip="203.0.113.98")).status == 429
    now[0] += 31
    assert (await _post_login(client, email, PASSWORD, ip="203.0.113.98")).status == 302


# --------------------------------------------------------------------------- Zwei-Schritt-Code
@pytest.fixture
def app2(tmp_path):
    return create_app(load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True, require_2fa=True), start_workers=False)


def _enable_totp(app, email) -> str:
    secret = totp.new_secret()
    app[DB].execute("UPDATE users SET totp_secret = ?, totp_enabled_at = ?, totp_last_step = NULL WHERE email = ?",
                    (crypto.encrypt(app[SETTINGS].secret_key, "totp", secret), utcnow(), email))
    return secret


async def test_app_code_login_and_no_reuse(aiohttp_client, app2):
    client = await aiohttp_client(app2)
    email = "therapie@demo.n-fizyo.ch"
    secret = _enable_totp(app2, email)
    r = await _post_login(client, email, PASSWORD)
    assert r.headers["Location"].startswith("/login/code")
    html = await (await client.get("/login/code")).text()
    code = totp.code_at(secret, totp.current_step())
    r = await client.post("/login/code", data={"_csrf": csrf_of(html), "code": code}, allow_redirects=False)
    assert r.status == 302 and r.headers["Location"].startswith("/therapie")
    assert (await client.get("/therapie/heute", allow_redirects=False)).status == 200
    assert app2[DB].value("SELECT COUNT(*) FROM audit_log WHERE action = 'login' AND detail LIKE '%totp%'") == 1
    # derselbe Code ein zweites Mal: abgelehnt
    client.session.cookie_jar.clear()
    await _post_login(client, email, PASSWORD)
    html = await (await client.get("/login/code")).text()
    r = await client.post("/login/code", data={"_csrf": csrf_of(html), "code": code}, allow_redirects=False)
    assert r.status == 200 and "stimmt nicht" in await r.text()
    assert (await client.get("/therapie/heute", allow_redirects=False)).status == 302


async def test_login_code_limit(aiohttp_client, app2):
    client = await aiohttp_client(app2)
    email = "empfang@demo.n-fizyo.ch"
    secret = _enable_totp(app2, email)
    await _post_login(client, email, PASSWORD)
    html = await (await client.get("/login/code")).text()
    for _ in range(8):
        r = await client.post("/login/code", data={"_csrf": csrf_of(html), "code": "000000"}, allow_redirects=False)
        assert r.status == 200
    good = totp.code_at(secret, totp.current_step())
    r = await client.post("/login/code", data={"_csrf": csrf_of(html), "code": good}, allow_redirects=False)
    assert r.status == 200 and "Zu viele Versuche" in await r.text()
    assert (await client.get("/empfang/heute", allow_redirects=False)).status == 302
    assert app2[DB].value("SELECT totp_last_step FROM users WHERE email = ?", (email,)) is None   # Code gar nicht geprüft


# --------------------------------------------------------------------------- Patienten-App: SMS-Code
async def _p_phone(client, phone, birthdate, ip=None):
    page = await client.get("/p/anmelden")
    headers = {"X-Forwarded-For": ip} if ip else {}
    return await client.post("/p/anmelden", data={"_csrf": csrf_of(await page.text()), "step": "phone", "phone": phone,
                                                  "birthdate": birthdate}, headers=headers)


def _sms_count(app, phone="+41790000008") -> int:
    """Login-Codes an diese Nummer (die Demo-Daten enthalten schon andere SMS)."""
    return app[DB].value("SELECT COUNT(*) FROM outbox WHERE channel = 'sms' AND recipient = ? AND body LIKE '%Kod%'", (phone,))


async def test_patient_sms_codes_are_capped(client, app):
    for _ in range(3):
        r = await _p_phone(client, **AYSE)
        assert r.status == 200 and 'name="lid"' in await r.text()
    assert _sms_count(app) == 3
    r = await _p_phone(client, **AYSE)
    assert r.status == 429 and "mehrere Codes" in await r.text()
    assert _sms_count(app) == 3
    # andere Schreibweise derselben Nummer, andere Adresse: zählt gleich
    r = await _p_phone(client, "+41 79 000 00 08", AYSE["birthdate"], ip="198.51.100.4")
    assert r.status == 429 and _sms_count(app) == 3


async def test_cap_message_does_not_reveal_patients(client, app):
    for _ in range(3):
        r = await _p_phone(client, "079 999 99 99", "1990-01-01")
        assert r.status == 200
    r = await _p_phone(client, "079 999 99 99", "1990-01-01")
    assert r.status == 429 and "mehrere Codes" in await r.text()
    assert _sms_count(app, "+41799999999") == 0


async def test_patient_cap_survives_restart_and_ip_cap(client, app):
    db = app[DB]
    ayse = pid_of(app, "Ayşe")
    for _ in range(3):   # schon 3 Codes in dieser Stunde (z. B. vor einem Neustart)
        db.execute("INSERT INTO patient_login_codes (practice_id, patient_id, code_hash, expires_at, created_at) VALUES (1,?,?,?,?)",
                   (ayse, "x", _iso(timedelta(minutes=15)), utcnow()))
    r = await _p_phone(client, **AYSE)
    assert r.status == 200 and 'name="lid"' in await r.text()     # Antwort wie immer …
    assert _sms_count(app) == 0                                   # … aber keine weitere SMS
    # pro Adresse höchstens 10 SMS-Codes pro Stunde
    app[THROTTLE].reset("p-sms-phone:790000008")
    for _ in range(10):
        app[THROTTLE].hit("p-sms-ip:198.51.100.20", 3600)
    db.execute("DELETE FROM patient_login_codes")
    r = await _p_phone(client, **AYSE, ip="198.51.100.20")
    assert r.status == 429 and _sms_count(app) == 0
    r = await _p_phone(client, **AYSE, ip="198.51.100.21")
    assert r.status == 200 and _sms_count(app) == 1


async def test_wrong_patient_codes(client, app):
    r = await _p_phone(client, **AYSE)
    text = await r.text()
    code = re.search(r"(\d{6})", re.search(r"banner info.*?</div>", text, re.S).group(0)).group(1)
    lid = re.search(r'name="lid" value="([^"]+)"', text).group(1)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        r = await client.post("/p/anmelden", data={"_csrf": csrf_of(text), "step": "code", "code": wrong, "lid": lid},
                              allow_redirects=False)
        assert r.status == 200 and "stimmt nicht" in await r.text()
    # nach 5 falschen Versuchen gilt auch der richtige Code nicht mehr
    r = await client.post("/p/anmelden", data={"_csrf": csrf_of(text), "step": "code", "code": code, "lid": lid}, allow_redirects=False)
    assert r.status == 200 and app[DB].value("SELECT COUNT(*) FROM sessions WHERE kind = 'patient'") == 0
    # pro Adresse: nach 8 Fehlversuchen gesperrt
    for _ in range(2):
        await client.post("/p/anmelden", data={"_csrf": csrf_of(text), "step": "code", "code": wrong, "lid": lid})
    r = await _p_phone(client, **AYSE)
    assert r.status == 200 and "Zu viele Versuche" in await r.text()
    # von einer anderen Adresse geht es (neuer Code)
    r = await _p_phone(client, **AYSE, ip="198.51.100.30")
    assert 'name="lid"' in await r.text()


async def test_patient_code_failures_counted_per_patient(client, app):
    """Falsche Codes für eine Person – egal von welcher Adresse – sperren nach 10 Versuchen in einer Stunde."""
    ayse = pid_of(app, "Ayşe")
    for _ in range(10):
        app[THROTTLE].fail(f"p-code:{ayse}", 3600)
    r = await _p_phone(client, **AYSE, ip="198.51.100.40")
    text = await r.text()
    code = re.search(r"(\d{6})", re.search(r"banner info.*?</div>", text, re.S).group(0)).group(1)
    lid = re.search(r'name="lid" value="([^"]+)"', text).group(1)
    r = await client.post("/p/anmelden", data={"_csrf": csrf_of(text), "step": "code", "code": code, "lid": lid},
                          headers={"X-Forwarded-For": "198.51.100.41"}, allow_redirects=False)
    assert r.status == 429 and app[DB].value("SELECT COUNT(*) FROM sessions WHERE kind = 'patient'") == 0


# --------------------------------------------------------------------------- Online-Termine: nur Handy CH/FL, SMS-Grenzen
def test_only_swiss_and_liechtenstein_mobiles():
    ok = ["+41791234567", "+41751234567", "+41741234567", "+4237901234", "+4236601234"]
    bad = ["+41311234567", "+41441234567", "+4232301234", "+4915123456789", "+33612345678", "+4179123456", None, ""]
    assert all(sms.is_ch_li_mobile(n) for n in ok)
    assert not any(sms.is_ch_li_mobile(n) for n in bad)
    assert sms.is_ch_li_mobile(sms.normalize_phone("00423 790 12 34"))


async def _booking_data(client, app, phone, pick, ip=None):
    url = "/p/termin/" + app[DB].value("SELECT slug FROM practices WHERE id = 1")
    html = await (await client.get(url)).text()
    type_id = re.search(r'href="\?art=(\d+)"', html).group(1)
    html = await (await client.get(f"{url}?art={type_id}")).text()
    slot = unescape(re.findall(r'class="slot" href="(\?art=\d+&(?:amp;)?s=[^"]+)"', html)[pick])
    html = await (await client.get(url + slot)).text()
    token = re.search(r'name="slot" value="([^"]+)"', html).group(1)
    headers = {"X-Forwarded-For": ip} if ip else {}
    return await client.post(url, headers=headers, data={
        "_csrf": csrf_of(html), "step": "data", "slot": token, "first_name": "Mira", "last_name": "Neu", "birthdate": "1985-02-03",
        "phone": phone, "consent": "1", "website": ""})


def _booking_sms(app) -> int:
    return app[DB].value("SELECT COUNT(*) FROM outbox WHERE channel = 'sms'")


async def test_booking_rejects_foreign_numbers(client, app):
    before = _booking_sms(app)
    r = await _booking_data(client, app, "+49 151 2345 6789", 0)
    assert r.status == 400 and "Liechtensteiner Handynummer" in await r.text()
    r = await _booking_data(client, app, "031 123 45 67", 0)          # Festnetz: keine SMS möglich
    assert r.status == 400
    assert _booking_sms(app) == before
    r = await _booking_data(client, app, "+423 790 12 34", 0)
    assert r.status == 200 and 'name="rid"' in await r.text() and _booking_sms(app) == before + 1


async def test_booking_sms_caps(client, app):
    before = _booking_sms(app)
    for i in range(3):
        r = await _booking_data(client, app, "079 555 44 33", i)
        assert r.status == 200
    r = await _booking_data(client, app, "079 555 44 33", 3, ip="198.51.100.50")   # andere Adresse, gleiche Nummer
    assert r.status == 429 and "mehrere Codes" in await r.text()
    assert _booking_sms(app) == before + 3
    # pro Adresse höchstens 6 Codes in einer Stunde (verschiedene Nummern)
    for i in range(6):
        r = await _booking_data(client, app, f"079 555 44 4{i}", 4 + i, ip="198.51.100.60")
        assert r.status == 200, i
    r = await _booking_data(client, app, "079 555 44 49", 10, ip="198.51.100.60")
    assert r.status == 429
    r = await _booking_data(client, app, "079 555 44 49", 10, ip="198.51.100.61")
    assert r.status == 200


# --------------------------------------------------------------------------- Sitzungen
def _staff_token(app, email) -> str:
    uid = app[DB].value("SELECT id FROM users WHERE email = ?", (email,))
    token, _ = create_session(app[DB], "staff", user_id=uid, practice_id=1)
    return token


async def _with_token(client, token, url):
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({"nf_s": token})
    return await client.get(url, allow_redirects=False)


async def test_session_ends_when_user_deactivated(client, app):
    therapist_token = _staff_token(app, "therapie@demo.n-fizyo.ch")
    assert (await _with_token(client, therapist_token, "/therapie/heute")).status == 200
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    uid = app[DB].value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    r = await client.post("/leitung/team", data={"_csrf": csrf, "do": "deactivate", "user_id": str(uid)}, allow_redirects=False)
    assert r.status == 302
    r = await _with_token(client, therapist_token, "/therapie/heute")
    assert r.status == 302 and r.headers["Location"].startswith("/login")
    assert app[DB].value("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid,)) == 0
    assert (await _post_login(client, "therapie@demo.n-fizyo.ch", PASSWORD)).status == 401


async def test_password_change_ends_other_sessions(client, app):
    other = _staff_token(app, "therapie@demo.n-fizyo.ch")
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    mine = client.session.cookie_jar.filter_cookies(client.make_url("/"))["nf_s"].value
    r = await client.post("/therapie/konto", data={"_csrf": csrf, "do": "password", "current": PASSWORD,
                                                   "new": "neues-passwort-123", "repeat": "neues-passwort-123"}, allow_redirects=False)
    assert r.status == 302
    assert (await _with_token(client, other, "/therapie/heute")).status == 302
    assert (await _with_token(client, mine, "/therapie/heute")).status == 200
    assert (await _post_login(client, "therapie@demo.n-fizyo.ch", "neues-passwort-123")).status == 302


async def test_idle_timeout_and_absolute_limit(client, app):
    db = app[DB]
    token = _staff_token(app, "empfang@demo.n-fizyo.ch")
    th = security.token_hash(token)
    db.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (_iso(-timedelta(hours=7)), th))
    assert (await _with_token(client, token, "/empfang/heute")).status == 200
    assert db.value("SELECT last_seen_at FROM sessions WHERE token_hash = ?", (th,)) >= _iso(-timedelta(minutes=1))   # gleitend
    db.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (_iso(-timedelta(hours=8, minutes=1)), th))
    r = await _with_token(client, token, "/empfang/heute")
    assert r.status == 302 and r.headers["Location"].startswith("/login")
    assert db.value("SELECT COUNT(*) FROM sessions WHERE token_hash = ?", (th,)) == 0
    # feste Höchstdauer gilt trotz Aktivität
    token = _staff_token(app, "empfang@demo.n-fizyo.ch")
    db.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?", (_iso(-timedelta(seconds=1)), security.token_hash(token)))
    assert (await _with_token(client, token, "/empfang/heute")).status == 302


async def test_patient_sessions_end_when_app_access_revoked(client, app):
    db = app[DB]
    ayse = pid_of(app, "Ayşe")
    t1, csrf1 = create_session(db, "patient", patient_id=ayse, practice_id=1)
    t2, _ = create_session(db, "patient", patient_id=ayse, practice_id=1)      # zweites Telefon
    client.session.cookie_jar.update_cookies({"nf_p": t1})
    assert (await client.get("/p/heute", allow_redirects=False)).status == 200
    r = await client.post("/p/profil/zugang-beenden", data={"_csrf": csrf1}, allow_redirects=False)
    assert r.status == 302
    for token in (t1, t2):
        client.session.cookie_jar.clear()
        client.session.cookie_jar.update_cookies({"nf_p": token})
        r = await client.get("/p/heute", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/p/anmelden"
    # ohne Einwilligung auch kein neuer SMS-Code
    client.session.cookie_jar.clear()
    await _p_phone(client, **AYSE)
    assert _sms_count(app) == 0


# --------------------------------------------------------------------------- Passwörter
def test_password_hash_production_strength(monkeypatch):
    monkeypatch.setattr(security, "SCRYPT_LOG_N", 17)          # echter Wert (Tests laufen sonst mit 2**14)
    stored = security.hash_password("geheim-123")
    assert stored.startswith("scrypt$ln=17,r=8,p=1$")
    assert security.verify_password("geheim-123", stored) and not security.verify_password("falsch", stored)
    assert not security.needs_rehash(stored)


def _legacy_hash(password: str) -> str:
    import base64
    import hashlib

    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


async def test_old_hash_is_upgraded_on_login(client, app, monkeypatch):
    monkeypatch.setattr(security, "SCRYPT_LOG_N", 15)
    db = app[DB]
    old = _legacy_hash(PASSWORD)
    assert security.verify_password(PASSWORD, old) and security.needs_rehash(old)
    db.execute("UPDATE users SET password_hash = ? WHERE email = 'empfang@demo.n-fizyo.ch'", (old,))
    assert (await _post_login(client, "empfang@demo.n-fizyo.ch", "falsch")).status == 401
    assert db.value("SELECT password_hash FROM users WHERE email = 'empfang@demo.n-fizyo.ch'") == old
    assert (await _post_login(client, "empfang@demo.n-fizyo.ch", PASSWORD)).status == 302
    new = db.value("SELECT password_hash FROM users WHERE email = 'empfang@demo.n-fizyo.ch'")
    assert new.startswith("scrypt$ln=15,r=8,p=1$") and security.verify_password(PASSWORD, new)


async def test_unknown_email_costs_the_same(client, app, monkeypatch):
    from nfizyo.core import auth

    calls = []
    monkeypatch.setattr(auth, "dummy_verify", lambda pw: calls.append(pw))
    assert (await _post_login(client, "gibt-es-nicht@demo.n-fizyo.ch", "x")).status == 401
    app[DB].execute("UPDATE users SET active = 0 WHERE email = 'empfang@demo.n-fizyo.ch'")
    assert (await _post_login(client, "empfang@demo.n-fizyo.ch", PASSWORD)).status == 401
    assert calls == ["x", PASSWORD]


# --------------------------------------------------------------------------- SMS: Codes nicht im Klartext gespeichert
@pytest.fixture
def live_app(tmp_path):
    settings = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True, sms_engine="live",
                             aspsms_userkey="KEY", aspsms_password="PW")
    return create_app(settings, start_workers=False)


async def test_codes_masked_after_sending_and_not_exported(aiohttp_client, live_app, monkeypatch):
    sent = []

    async def fake(url, payload, headers=None):
        sent.append(payload["MessageText"])
        return 200, {"StatusCode": "1"}

    monkeypatch.setattr(sms, "http_post_json", fake)
    db = live_app[DB]
    sms.set_provider(db, 1, "aspsms", "Physio")
    # vor dem Versand: Klartext nur in body_secret
    mid = live_app[SMS].send(1, "sms", "079 123 45 67", "Ihr Code: 482913 – Link https://x.invalid/p/einladung/TOKENabc123",
                             secrets=("482913", "TOKENabc123"))
    row = db.one("SELECT body, body_secret FROM outbox WHERE id = ?", (mid,))
    assert "482913" not in row["body"] and "TOKENabc123" not in row["body"] and "/p/einladung/••••••" in row["body"]
    assert "482913" in row["body_secret"]
    await asyncio.sleep(0.05)
    row = db.one("SELECT status, body, body_secret FROM outbox WHERE id = ?", (mid,))
    assert row["status"] == "sent" and row["body_secret"] is None and "482913" in sent[0]
    # Anmeldung in der Patienten-App: Code geht an den Anbieter, bleibt aber nicht in der Datenbank
    client = await aiohttp_client(live_app)
    r = await _p_phone(client, **AYSE)
    code = re.search(r"(\d{6})", re.search(r"banner info.*?</div>", await r.text(), re.S).group(0)).group(1)
    await asyncio.sleep(0.05)
    assert code in sent[-1]
    dump = " ".join(str(tuple(r)) for r in db.query("SELECT * FROM outbox"))
    assert code not in dump and "482913" not in dump
    # Datenexport: kein body_secret, keine Codes
    path = core_export.build(db, live_app[SETTINGS], 1)
    with zipfile.ZipFile(path) as zf:
        outbox_csv = zf.read("tabellen/outbox.csv").decode("utf-8")
    assert "body_secret" not in outbox_csv and code not in outbox_csv and "482913" not in outbox_csv


async def test_secret_kept_until_final_failure(live_app, monkeypatch):
    async def fail(url, payload, headers=None):
        return 500, {}

    monkeypatch.setattr(sms, "http_post_json", fail)
    db = live_app[DB]
    sms.set_provider(db, 1, "aspsms", None)
    service = live_app[SMS]
    mid = service.send(1, "sms", "+41791234567", "Code 135792", secrets=("135792",))
    await asyncio.sleep(0.05)
    assert db.value("SELECT body_secret FROM outbox WHERE id = ?", (mid,)) == "Code 135792"   # Wiederholung braucht den Text
    for _ in range(sms.MAX_ATTEMPTS):
        db.execute("UPDATE outbox SET next_try_at = NULL WHERE id = ?", (mid,))
        await service.deliver(mid)
    row = db.one("SELECT status, body, body_secret FROM outbox WHERE id = ?", (mid,))
    assert row["status"] == "failed" and row["body_secret"] is None and "135792" not in row["body"]


def test_log_mode_outside_demo_stores_no_code(tmp_path):
    app = create_app(load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=False), start_workers=False, seed_demo=False)
    db = app[DB]
    pid = db.execute("INSERT INTO practices (name, slug, created_at) VALUES ('P', 'p', ?)", (utcnow(),))
    mid = app[SMS].send(pid, "sms", "079 123 45 67", "Ihr Anmeldecode: 246810", secrets=("246810",))
    row = db.one("SELECT status, body, body_secret FROM outbox WHERE id = ?", (mid,))
    assert row["status"] == "logged" and row["body"] == "Ihr Anmeldecode: ••••••" and row["body_secret"] is None


# --------------------------------------------------------------------------- CSV / Excel: keine Formeln
def test_safe_cell():
    assert safe_cell("=HYPERLINK(\"http://x\",\"klick\")") == "'=HYPERLINK(\"http://x\",\"klick\")"
    for bad in ("+41+cmd", "-2+3+cmd|' /C calc'!A0", "@SUM(A1)", "\tx", "\rx", "＝1+1"):
        assert safe_cell(bad).startswith("'"), bad
    for keep in ("Müller", "-12.50", "+41", "1'290.00", "", None, 12, -3.5, True):
        assert safe_cell(keep) == keep
    assert safe_row(["=1", 2.0]) == ["'=1", 2.0]


def _journal_data():
    from nfizyo.modules.exports.journal import Entry

    return {"entries": [Entry("2026-11-02", "Z-1", "1020", "1100", -12.5, "=HYPERLINK(\"http://evil\",\"x\")"),
                        Entry("2026-11-02", "R-1", "1100", "3400", 90.0, "Rechnung R-1")],
            "invoices": [], "cancellations": [], "sales": [], "firms": {"invoices": [], "payments": []},
            "payments": [{"booked_on": "2026-11-02", "number": "R-1", "amount_chf": 90.0, "method": "bank", "source": "camt",
                          "debtor": "=cmd|' /C calc'!A0", "reference": "@ref"}]}


def test_journal_csv_and_excel_are_neutralised():
    from openpyxl import load_workbook

    from nfizyo.modules.exports import journal

    rows = list(csv.reader(io.StringIO(journal.journal_csv(_journal_data()).decode("utf-8-sig")), delimiter=";"))
    assert rows[1][5].startswith("'=HYPERLINK") and rows[1][4] == "-12.50" and rows[2][5] == "Rechnung R-1"
    wb = load_workbook(io.BytesIO(journal.workbook(_journal_data(), [], [])))
    j = wb["Journal"]
    assert j["F2"].value.startswith("'=HYPERLINK") and j["F2"].data_type == "s" and j["E2"].value == -12.5
    z = wb["Zahlungen"]
    assert z["F2"].value == "'=cmd|' /C calc'!A0" and z["G2"].value == "'@ref" and z["C2"].value == 90.0
    assert all(c.data_type != "f" for ws in wb.worksheets for row in ws.iter_rows() for c in row)


async def test_full_export_neutralises_text(client, app):
    db = app[DB]
    db.execute("UPDATE patients SET last_name = ? WHERE id = ?", ("=1+2", pid_of(app, "Lea")))
    path = core_export.build(db, app[SETTINGS], 1)
    with zipfile.ZipFile(path) as zf:
        rows = list(csv.reader(io.StringIO(zf.read("tabellen/patients.csv").decode("utf-8-sig")), delimiter=";"))
    names = [r[rows[0].index("last_name")] for r in rows[1:]]
    assert "'=1+2" in names and "=1+2" not in names


# --------------------------------------------------------------------------- PDF-Text in eigenem Prozess
def _pdf(tmp_path, text="Bericht Physiotherapie Knie links"):
    from reportlab.pdfgen import canvas

    p = tmp_path / "bericht.pdf"
    c = canvas.Canvas(str(p))
    c.drawString(100, 700, text)
    c.showPage()
    c.save()
    return p


def test_pdf_text_runs_isolated_with_limits(tmp_path, monkeypatch):
    from nfizyo.ai import ocr

    p = _pdf(tmp_path)
    text, pages = ocr._pdf_text(p)
    assert "Physiotherapie" in text and pages == 1
    (tmp_path / "kaputt.pdf").write_bytes(b"%PDF-1.4 kaputt")
    assert ocr._pdf_text(tmp_path / "kaputt.pdf") == ("", 0)
    # Speichergrenze greift (zu wenig Speicher → abgebrochen, kein Absturz der App)
    monkeypatch.setattr(ocr, "PDF_MEMORY_BYTES", 20 * 1024 * 1024)
    assert ocr._pdf_text(p) == ("", 0)
    # Zeitgrenze: ein hängender Prozess wird beendet
    monkeypatch.setattr(ocr, "PDF_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(ocr, "_PDF_CHILD", "import time; time.sleep(30)")
    start = time.monotonic()
    assert ocr._pdf_text(p) == ("", 0)
    assert time.monotonic() - start < 10


def test_requirements_pin_pypdf_and_lock_has_hashes():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    assert "pypdf>=6.21.4" in (root / "requirements.txt").read_text()
    lock = (root / "requirements.lock").read_text()
    pins = re.findall(r"^([a-z0-9_.-]+)==([^\s\\]+)", lock, re.M)
    assert ("pypdf", "6.21.4") in pins or any(n == "pypdf" and tuple(map(int, v.split(".")[:3])) >= (6, 21, 4) for n, v in pins)
    assert lock.count("--hash=sha256:") >= len(pins) and len(pins) > 10
    for script in ("install.sh", "update.sh"):
        assert "--require-hashes -r" in (root / "deploy" / script).read_text()


def test_settings_namespace_without_demo_flag_is_ok():
    """SmsService arbeitet auch mit einfachen Einstellungs-Objekten (z. B. Befehlszeile) ohne demo-Feld."""
    from nfizyo.core.db import Database

    db = Database(":memory:")
    db.conn.executescript("CREATE TABLE practices (id INTEGER PRIMARY KEY, sms_provider TEXT, sms_sender TEXT, name TEXT);"
                          "CREATE TABLE outbox (id INTEGER PRIMARY KEY, practice_id INTEGER, channel TEXT, recipient TEXT, body TEXT,"
                          " body_secret TEXT, status TEXT, created_at TEXT, provider TEXT, error TEXT);"
                          "INSERT INTO practices (id, sms_provider, name) VALUES (1, 'log', 'P');")
    service = sms.SmsService(SimpleNamespace(sms_engine="log", aspsms_userkey="", aspsms_password="", ecall_username="",
                                             ecall_password=""), db)
    mid = service.send(1, "sms", "+41791234567", "Code 975310", secrets=("975310",))
    assert db.value("SELECT body FROM outbox WHERE id = ?", (mid,)) == "Code ••••••"
