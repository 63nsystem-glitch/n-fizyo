"""Zwei-Schritt-Anmeldung (Authenticator-App): Pflicht für alle Praxiskonten, ohne Code kein Zugang."""
from __future__ import annotations

import re

import pytest
from cryptography.exceptions import InvalidTag

from nfizyo.app import create_app
from nfizyo.core import crypto, mfa, totp
from nfizyo.core.config import load_settings
from nfizyo.core.keys import DB, SETTINGS

from conftest import PASSWORD, csrf_of


def test_totp_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"
    assert totp.code_at(secret, 59 // 30) == "287082"            # RFC 6238 Anhang B (SHA1, 8 → 6 Stellen)
    assert totp.code_at(secret, 1111111109 // 30) == "081804"
    assert totp.verify(secret, "287082", now=59) == 1
    assert totp.verify(secret, "287082", last_step=1, now=59) is None   # Wiederverwendung abgelehnt
    assert totp.verify(secret, "000000", now=59) is None


def test_secret_encrypted_and_bound_to_purpose():
    token = crypto.encrypt(b"k" * 32, "totp", "GEZDGNBV")
    assert "GEZDGNBV" not in token
    assert crypto.decrypt(b"k" * 32, "totp", token) == "GEZDGNBV"
    with pytest.raises(InvalidTag):
        crypto.decrypt(b"k" * 32, "other", token)
    with pytest.raises(InvalidTag):
        crypto.decrypt(b"x" * 32, "totp", token)


def test_recovery_codes_single_use():
    codes = totp.new_recovery_codes()
    hashes = [totp.hash_recovery(c) for c in codes]
    left = totp.use_recovery(hashes, codes[3].upper().replace("-", " "))
    assert left is not None and len(left) == 9
    assert totp.use_recovery(left, codes[3]) is None


@pytest.fixture
def app2(tmp_path):
    settings = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True, require_2fa=True)
    return create_app(settings, start_workers=False)


async def _password(client, email):
    client.session.cookie_jar.clear()
    r = await client.get("/login")
    return await client.post("/login", data={"_csrf": csrf_of(await r.text()), "email": email, "password": PASSWORD},
                             allow_redirects=False)


def _code_for(app, email, pending=False):
    db = app[DB]
    user = db.one("SELECT * FROM users WHERE email = ?", (email,))
    secret = crypto.decrypt(app[SETTINGS].secret_key, "totp", user["totp_pending" if pending else "totp_secret"])
    return totp.code_at(secret, totp.current_step())


async def test_first_login_requires_setup_then_code(aiohttp_client, app2):
    client = await aiohttp_client(app2)
    email = "therapie@demo.n-fizyo.ch"
    r = await _password(client, email)
    assert r.status == 302 and r.headers["Location"].startswith("/sicherheit/einrichten")
    # Zwischenstufe ist KEINE Anmeldung
    r2 = await client.get("/therapie/heute", allow_redirects=False)
    assert r2.status == 302 and r2.headers["Location"].startswith("/login")
    page = await client.get("/sicherheit/einrichten")
    html = await page.text()
    assert "<svg" in html
    # falscher Code
    bad = await client.post("/sicherheit/einrichten", data={"_csrf": csrf_of(html), "code": "000000"})
    assert "stimmt nicht" in await bad.text()
    ok = await client.post("/sicherheit/einrichten", data={"_csrf": csrf_of(html), "code": _code_for(app2, email, pending=True)})
    text = await ok.text()
    codes = re.findall(r"<li>([a-z0-9]{5}-[a-z0-9]{5})</li>", text)
    assert len(codes) == 10
    assert (await client.get("/therapie/heute", allow_redirects=False)).status == 200
    user = app2[DB].one("SELECT * FROM users WHERE email = ?", (email,))
    assert user["totp_enabled_at"] and not user["totp_pending"] and codes[0] not in (user["recovery_codes"] or "")

    # Zweites Anmelden: Code nötig
    r = await _password(client, email)
    assert r.headers["Location"].startswith("/login/code")
    assert (await client.get("/therapie/heute", allow_redirects=False)).status == 302
    page = await client.get("/login/code")
    html = await page.text()
    # derselbe Code wie bei der Einrichtung gilt nicht noch einmal (Zeitfenster schon benutzt) → Wiederherstellungscode
    r = await client.post("/login/code", data={"_csrf": csrf_of(html), "code": codes[0]}, allow_redirects=False)
    assert r.status == 302 and "/konto/sicherheit?recovery=1" in r.headers["Location"]
    assert (await client.get("/therapie/heute", allow_redirects=False)).status == 200
    # Wiederherstellungscode gilt nur einmal
    r = await _password(client, email)
    page = await client.get("/login/code")
    r = await client.post("/login/code", data={"_csrf": csrf_of(await page.text()), "code": codes[0]}, allow_redirects=False)
    assert r.status == 200
    assert (await client.get("/therapie/heute", allow_redirects=False)).status == 302


async def test_pending_session_cannot_post(aiohttp_client, app2):
    client = await aiohttp_client(app2)
    await _password(client, "empfang@demo.n-fizyo.ch")
    page = await client.get("/sicherheit/einrichten")
    r = await client.post("/empfang/patienten/neu", data={"_csrf": csrf_of(await page.text()), "first_name": "X", "last_name": "Y"},
                          allow_redirects=False)
    assert r.status in (302, 403, 404)
    assert r.status != 302 or r.headers["Location"].startswith("/login")


async def test_owner_can_reset_and_user_must_setup_again(aiohttp_client, app2):
    db = app2[DB]
    therapist = db.one("SELECT * FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    mfa.begin_setup(db, app2[SETTINGS].secret_key, therapist["id"])
    mfa.confirm_setup(db, app2[SETTINGS].secret_key, db.one("SELECT * FROM users WHERE id = ?", (therapist["id"],)),
                      _code_for(app2, therapist["email"], pending=True))
    owner = db.one("SELECT * FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    mfa.begin_setup(db, app2[SETTINGS].secret_key, owner["id"])
    mfa.confirm_setup(db, app2[SETTINGS].secret_key, db.one("SELECT * FROM users WHERE id = ?", (owner["id"],)),
                      _code_for(app2, owner["email"], pending=True))
    client = await aiohttp_client(app2)
    await _password(client, owner["email"])
    page = await client.get("/login/code")
    # Das aktuelle Zeitfenster ist durch die Einrichtung schon benutzt → mit Wiederherstellungscode anmelden
    codes = mfa.new_recovery_codes(db, db.one("SELECT * FROM users WHERE id = ?", (owner["id"],)))
    r = await client.post("/login/code", data={"_csrf": csrf_of(await page.text()), "code": codes[0]}, allow_redirects=False)
    assert r.status == 302
    team = await client.get("/leitung/team")
    html = await team.text()
    assert "2FA aktiv" in html
    r = await client.post("/leitung/team", data={"_csrf": csrf_of(html), "do": "mfa_reset", "user_id": str(therapist["id"])},
                          allow_redirects=False)
    assert r.status == 302
    after = db.one("SELECT * FROM users WHERE id = ?", (therapist["id"],))
    assert after["totp_secret"] is None and after["recovery_codes"] is None
    assert db.value("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (therapist["id"],)) == 0
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE action = 'mfa_reset' AND entity_id = ?", (therapist["id"],)) == 1


async def test_other_practice_cannot_reset(aiohttp_client, app, practice_b):
    db = app[DB]
    client = await aiohttp_client(app)
    target = db.one("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")["id"]
    db.execute("UPDATE users SET totp_secret = 'x', totp_enabled_at = '2026-01-01' WHERE id = ?", (target,))
    db.execute("UPDATE users SET totp_secret = NULL WHERE email = 'chef@praxis-b.invalid'")
    client.session.cookie_jar.clear()
    from nfizyo.core.auth import create_session
    token, csrf = create_session(db, "staff", user_id=practice_b["user"], practice_id=practice_b["id"])
    client.session.cookie_jar.update_cookies({"nf_s": token})
    r = await client.post("/leitung/team", data={"_csrf": csrf, "do": "mfa_reset", "user_id": str(target)}, allow_redirects=False)
    assert r.status == 302
    assert db.one("SELECT totp_secret FROM users WHERE id = ?", (target,))["totp_secret"] == "x"


async def test_new_phone_needs_password(aiohttp_client, app2):
    """Eine übernommene Sitzung allein reicht nicht, um die Zwei-Schritt-Anmeldung auf ein anderes Telefon umzustellen."""
    db = app2[DB]
    user = db.one("SELECT * FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    mfa.begin_setup(db, app2[SETTINGS].secret_key, user["id"])
    mfa.confirm_setup(db, app2[SETTINGS].secret_key, db.one("SELECT * FROM users WHERE id = ?", (user["id"],)),
                      _code_for(app2, user["email"], pending=True))
    before = db.value("SELECT totp_secret FROM users WHERE id = ?", (user["id"],))
    codes = mfa.new_recovery_codes(db, db.one("SELECT * FROM users WHERE id = ?", (user["id"],)))
    client = await aiohttp_client(app2)
    await _password(client, user["email"])
    page = await client.get("/login/code")
    await client.post("/login/code", data={"_csrf": csrf_of(await page.text()), "code": codes[1]})
    # direkt zur Einrichtung ohne Passwort → zurück zu «Mein Konto», kein neuer Schlüssel
    r = await client.get("/sicherheit/einrichten?neu=1", allow_redirects=False)
    assert r.status == 302 and "/konto/sicherheit" in r.headers["Location"]
    assert db.value("SELECT totp_pending FROM users WHERE id = ?", (user["id"],)) is None
    html = await (await client.get("/therapie/konto/sicherheit")).text()
    r = await client.post("/therapie/konto/sicherheit", data={"_csrf": csrf_of(html), "do": "new_phone", "password": "falsch"},
                          allow_redirects=False)
    assert r.status == 200 and db.value("SELECT totp_pending FROM users WHERE id = ?", (user["id"],)) is None
    r = await client.post("/therapie/konto/sicherheit", data={"_csrf": csrf_of(html), "do": "new_phone", "password": PASSWORD},
                          allow_redirects=False)
    assert r.status == 302 and "t=" in r.headers["Location"]
    page = await client.get(r.headers["Location"])
    html = await page.text()
    token = re.search(r'name="t" value="([^"]+)"', html).group(1)
    await client.post("/sicherheit/einrichten", data={"_csrf": csrf_of(html), "t": token, "code": _code_for(app2, user["email"], pending=True)})
    assert db.value("SELECT totp_secret FROM users WHERE id = ?", (user["id"],)) != before
