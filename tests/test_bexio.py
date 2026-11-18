"""bexio: Journal als manuelle Buchungen senden – Konten über die Kontonummer, keine Doppel, unklare Antworten nicht blind
wiederholen, OAuth mit verschlüsseltem Refresh-Token. bexio selbst ist hier ein Testserver."""
from __future__ import annotations

import json
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

from aiohttp import web
from conftest import login

from nfizyo.app import create_app
from nfizyo.core.config import load_settings
from nfizyo.core.keys import DB

TOKEN = "pat-test-" + "x" * 20
ACCOUNTS = {"1000": 11, "1020": 12, "1091": 13, "1100": 14, "3400": 34, "3600": 36, "2200": 22}   # 3200 (Produkte) fehlt


def fake_bexio(state: dict) -> web.Application:
    async def accounts(request):
        state.setdefault("auth", []).append(request.headers.get("Authorization"))
        if state.get("expired"):
            return web.json_response({"error_code": 401, "message": "Unauthorized"}, status=401)
        return web.json_response([{"id": i, "account_no": n, "name": f"Konto {n}", "is_active": True} for n, i in ACCOUNTS.items()])

    async def currencies(request):
        return web.json_response([{"id": 1, "name": "CHF", "round_factor": 0.05}, {"id": 2, "name": "EUR"}])

    async def manual(request):
        body = await request.json()
        state.setdefault("posted", []).append({"auth": request.headers.get("Authorization"), **body})
        if state.get("fail_doc") and body["reference_nr"] == state["fail_doc"]:
            return web.json_response({"error_code": 500, "message": "Server Error"}, status=500)
        return web.json_response({"id": 1000 + len(state["posted"]), **body}, status=201)

    async def token(request):
        form = await request.post()
        state.setdefault("token_calls", []).append(dict(form))
        if form.get("grant_type") == "authorization_code" and form.get("code") == "good-code":
            return web.json_response({"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 3600})
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") in ("ref-1", "ref-2"):
            return web.json_response({"access_token": "acc-2", "refresh_token": "ref-2", "expires_in": 3600})
        return web.json_response({"error": "invalid_grant", "error_description": "bad"}, status=400)

    app = web.Application()
    app.router.add_get("/2.0/accounts", accounts)
    app.router.add_get("/3.0/currencies", currencies)
    app.router.add_post("/3.0/accounting/manual_entries", manual)
    app.router.add_post("/auth/token", token)
    return app


async def _client(aiohttp_client, aiohttp_server, tmp_path, state, **over):
    server = await aiohttp_server(fake_bexio(state))
    url = str(server.make_url("")).rstrip("/")
    settings = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True, bexio_api=url, bexio_auth=url + "/auth", **over)
    app = create_app(settings, start_workers=False)
    return await aiohttp_client(app), app


def _period() -> dict:
    t = date.today()
    return {"von": (t - timedelta(days=40)).isoformat(), "bis": t.isoformat()}


async def test_send_with_token(aiohttp_client, aiohttp_server, tmp_path):
    state: dict = {}
    client, app = await _client(aiohttp_client, aiohttp_server, tmp_path, state, bexio_token=TOKEN)
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    page = await (await client.get("/leitung/einstellungen/export?von={von}&bis={bis}".format(**_period()))).text()
    assert "Mit bexio verbunden" in page and TOKEN not in page
    # Verbindung prüfen: fehlendes Konto 3200 wird gemeldet
    r = await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_check", **_period()}, allow_redirects=False)
    assert "bexio_fehlt=3200" in r.headers["Location"]
    assert state["auth"][-1] == f"Bearer {TOKEN}"
    # Senden
    await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_send", **_period()})
    posted = state["posted"]
    assert posted and all(p["type"] == "manual_single_entry" and p["auth"] == f"Bearer {TOKEN}" for p in posted)
    e = posted[0]["entries"][0]
    assert e["currency_id"] == 1 and e["currency_factor"] == 1 and e["debit_account_id"] in ACCOUNTS.values()
    assert e["credit_account_id"] in ACCOUNTS.values() and e["amount"] > 0 and posted[0]["reference_nr"] and posted[0]["date"]
    sent = db.value("SELECT COUNT(*) FROM bexio_bookings WHERE status = 'sent'")
    failed = db.query("SELECT * FROM bexio_bookings WHERE status = 'failed'")
    assert sent == len(posted)
    assert all("3200" in f["error"] for f in failed)                       # Zeilen mit Produktkonto: abgelehnt, nicht gesendet
    assert all(p["entries"][0]["credit_account_id"] != 32 for p in posted)
    # Zweites Senden: nichts doppelt
    n = len(posted)
    r = await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_send", **_period()}, allow_redirects=False)
    assert len(state["posted"]) == n
    page = await (await client.get(r.headers["Location"])).text()
    assert "Konto fehlt in bexio" in page or not failed


async def test_unclear_answer_stops_and_is_marked(aiohttp_client, aiohttp_server, tmp_path):
    state: dict = {}
    client, app = await _client(aiohttp_client, aiohttp_server, tmp_path, state, bexio_token=TOKEN)
    db = app[DB]
    ACCOUNTS["3200"] = 32
    try:
        csrf = await login(client, "leitung@demo.n-fizyo.ch")
        from nfizyo.core.tenant import practice_scope
        from nfizyo.modules.billing import repo as billing
        from nfizyo.modules.cashdesk import api as cash_api
        from nfizyo.modules.exports import journal
        from nfizyo.modules.firms import api as firms_api

        with practice_scope(1, 1):
            t = date.today()
            for amount in (40.0, 60.0):                                   # zwei nicht zugeordnete Zahlungen → mehr Zeilen
                billing.add_payment(db, amount=amount, booked_on=t.isoformat(), source="manual", debtor="Test")
            d0 = t - timedelta(days=40)
            lo, hi = journal.bounds(d0, t)
            entries = journal.build(db, d0, t, cash_sales=cash_api.accounting_sales(db, d0.isoformat(), t.isoformat()),
                                    firms=firms_api.accounting(db, lo, hi, d0.isoformat(), t.isoformat()))["entries"]
        assert len(entries) >= 3
        docs = [e.doc for e in entries]
        idx = next(i for i, d in enumerate(docs) if 0 < i < len(docs) - 1 and docs.count(d) == 1)
        state["fail_doc"] = docs[idx]
        await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_send", **_period()})
        unknown = db.query("SELECT * FROM bexio_bookings WHERE status = 'unknown'")
        assert len(unknown) == 1 and unknown[0]["doc"] == docs[idx]
        posted_before = len(state["posted"])
        assert posted_before == idx + 1 < len(entries)                    # nach der Störung nicht weiter gesendet
        # Erneut senden: die unklare Zeile wird NICHT wiederholt, der Rest schon
        state["fail_doc"] = None
        await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_send", **_period()})
        assert all(p["reference_nr"] != docs[idx] for p in state["posted"][posted_before:])
        assert db.value("SELECT status FROM bexio_bookings WHERE id = ?", (unknown[0]["id"],)) == "unknown"
        # Leitung hat in bexio geprüft: «fehlt – neu senden», dann wird sie gesendet
        await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_mark", "booking_id": str(unknown[0]["id"]),
                                                                 "status": "failed", **_period()})
        await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_send", **_period()})
        assert db.value("SELECT status FROM bexio_bookings WHERE id = ?", (unknown[0]["id"],)) == "sent"
        assert db.value("SELECT COUNT(*) FROM bexio_bookings WHERE status != 'sent'") == 0
    finally:
        ACCOUNTS.pop("3200", None)


async def test_oauth_connect_refresh_disconnect(aiohttp_client, aiohttp_server, tmp_path):
    state: dict = {}
    client, app = await _client(aiohttp_client, aiohttp_server, tmp_path, state, bexio_client_id="nfizyo-app",
                                bexio_client_secret="s" * 32)
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    assert "Mit bexio verbinden" in await (await client.get("/leitung/einstellungen/export")).text()
    r = await client.get("/leitung/einstellungen/export/bexio/verbinden", allow_redirects=False)
    loc = urlparse(r.headers["Location"])
    q = parse_qs(loc.query)
    assert loc.path.endswith("/auth/auth") and q["client_id"] == ["nfizyo-app"] and "offline_access" in q["scope"][0]
    assert q["redirect_uri"][0].endswith("/leitung/einstellungen/export/bexio/zurueck")
    # falscher Zustand → abgelehnt
    r = await client.get("/leitung/einstellungen/export/bexio/zurueck?code=good-code&state=falsch", allow_redirects=False)
    assert db.value("SELECT refresh_token_enc FROM bexio_connections") is None
    # richtiger Ablauf (neuer Zustand, da der alte verbraucht ist)
    r = await client.get("/leitung/einstellungen/export/bexio/verbinden", allow_redirects=False)
    st = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
    await client.get(f"/leitung/einstellungen/export/bexio/zurueck?code=good-code&state={st}")
    enc = db.value("SELECT refresh_token_enc FROM bexio_connections")
    assert enc and "ref-1" not in enc                                     # verschlüsselt gespeichert
    assert state["token_calls"][0]["client_secret"] == "s" * 32 and state["token_calls"][0]["redirect_uri"].endswith("/zurueck")
    await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_check", **_period()})
    assert state["auth"][-1] == "Bearer acc-1"
    # Zugriffstoken abgelaufen (nur im Speicher) → Refresh-Token wird benutzt und erneuert
    from nfizyo.modules.exports import bexio

    bexio._TOKENS.clear()
    await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_check", **_period()})
    assert state["auth"][-1] == "Bearer acc-2" and state["token_calls"][-1]["grant_type"] == "refresh_token"
    assert db.value("SELECT refresh_token_enc FROM bexio_connections") != enc
    # Trennen
    await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_disconnect"})
    assert db.value("SELECT refresh_token_enc FROM bexio_connections") is None
    assert "Mit bexio verbinden" in await (await client.get("/leitung/einstellungen/export")).text()


async def test_expired_token_and_other_roles(aiohttp_client, aiohttp_server, tmp_path):
    state: dict = {"expired": True}
    client, app = await _client(aiohttp_client, aiohttp_server, tmp_path, state, bexio_token=TOKEN)
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    r = await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_send", **_period()}, allow_redirects=False)
    assert "posted" not in state and db.value("SELECT COUNT(*) FROM bexio_bookings") == 0
    page = await (await client.get(r.headers["Location"])).text()
    assert "nicht erreichbar" in page or "abgelehnt" in page
    # Empfang und Therapie: keine Export-Seite
    for email in ("empfang@demo.n-fizyo.ch", "therapie@demo.n-fizyo.ch"):
        csrf = await login(client, email)
        for path in ("/leitung/einstellungen/export", "/leitung/einstellungen/export/bexio/verbinden"):
            assert (await client.get(path, allow_redirects=False)).status in (302, 403, 404)


def test_entry_key_is_stable():
    from nfizyo.modules.exports.bexio import entry_key
    from nfizyo.modules.exports.journal import Entry

    a = Entry("2026-10-01", "2026-0001", "1100", "3400", 120.0, "Rechnung 2026-0001")
    b = Entry("2026-10-01", "2026-0001", "1100", "3400", 120.0, "Rechnung 2026-0001")
    c = Entry("2026-10-01", "2026-0001", "1100", "3400", 120.01, "Rechnung 2026-0001")
    assert entry_key(a) == entry_key(b) != entry_key(c)
    assert json.dumps(entry_key(a))
