"""bexio: das Buchungsjournal (siehe journal.py) als «manuelle Buchungen» an bexio senden.

Warum manuelle Buchungen und nicht bexio-Rechnungen: N-Fizyo stellt die Rechnungen selbst aus (Nummer, QR-Referenz, PDF) und
führt die offenen Posten. In bexio landen nur die Buchungen – keine zweiten Rechnungen, keine Patientendaten ausser dem
Buchungstext («Rechnung 2026-0042 Muster Lea») wie im CSV-Journal.

Zugang (nur in der .env, nie im Chat): NFIZYO_BEXIO_TOKEN (Personal Access Token, 60 Tage gültig) gehört EINER Praxis:
NFIZYO_BEXIO_PRACTICE (Kurzname oder Nummer; leer = die einzige Praxis der Box). Alle anderen Praxen der Box sehen bexio als
«nicht verfügbar» – ihr Journal kann nie in die Buchhaltung einer anderen Praxis gelangen. Oder NFIZYO_BEXIO_CLIENT_ID / _SECRET
(OAuth-App von NSystem; jede Leitung verbindet einmal ihr eigenes bexio, das Refresh-Token liegt verschlüsselt in der Datenbank).

Doppelte Buchungen vermeiden: jede Journalzeile hat einen festen Schlüssel aus ihrer Herkunft (Entry.src, z. B. «pay:17» =
Zahlung 17, «inv:12:services» = Ertrag Leistungen der Rechnung 12) – nie aus Namen oder Text. Umbenennen einer Firma, Korrektur
eines Patientennamens, Zusammenführen usw. schickt deshalb nichts ein zweites Mal. Gesendete Zeilen werden nie wieder gesendet.
Zeilen, die vor 0.9.0 mit dem alten Schlüssel (Datum|Beleg|Konten|Betrag|Text) gesendet wurden, werden über Datum, Beleg,
Konten und Betrag (ohne Text) wiedererkannt und bekommen den neuen Schlüssel (je alte Zeile höchstens einmal).
Bei Zeitüberschreitung oder Serverfehler weiss man nicht, ob bexio gebucht hat → Status «unklar», kein automatischer neuer
Versuch; die Leitung prüft in bexio und markiert.

MWST: die MWST-Zeilen des Journals gehen auf das MWST-Konto (z. B. 2200), ohne bexio-Steuercode. Wer die MWST-Abrechnung in
bexio macht, klärt das mit der Treuhand (die meisten Physiotherapie-Leistungen sind von der MWST ausgenommen).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import aiohttp

from ...core import crypto
from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from .journal import Entry

SCOPES = "openid offline_access accounting"
PURPOSE = "bexio-refresh-token"
TIMEOUT = aiohttp.ClientTimeout(total=20)
_TOKENS: dict[int, tuple[str, float]] = {}        # practice_id → (access token, gültig bis) – nur im Speicher


class BexioError(Exception):
    """Klare Antwort von bexio (z. B. 401, 422) – Text für die Anzeige."""


class Unclear(Exception):
    """Keine klare Antwort (Zeitüberschreitung, 5xx) – ob gebucht wurde, ist offen."""


# --------------------------------------------------------------------------- Zugang
def mode(settings) -> str:
    """Einrichtung der Box: token | oauth | off. Für eine Praxis gilt mode_here()."""
    if settings.bexio_token:
        return "token"
    if settings.bexio_client_id and settings.bexio_client_secret:
        return "oauth"
    return "off"


def token_practice_id(db: Database, settings) -> int | None:
    """Praxis, der NFIZYO_BEXIO_TOKEN gehört: NFIZYO_BEXIO_PRACTICE (Kurzname oder Nummer), sonst die einzige Praxis der Box.
    None = bei mehreren Praxen nicht festgelegt (dann nutzt niemand das Token)."""
    want = (getattr(settings, "bexio_practice", "") or "").strip()
    if want:
        pid = db.value("SELECT id FROM practices WHERE slug = ?", (want,))
        if pid is None and want.isdigit() and len(want) <= 12:
            pid = db.value("SELECT id FROM practices WHERE id = ?", (int(want),))
        return pid
    ids = [r["id"] for r in db.query("SELECT id FROM practices ORDER BY id LIMIT 2")]
    return ids[0] if len(ids) == 1 else None


def mode_here(db: Database, settings) -> str:
    """Für die aktuelle Praxis: token (nur die Praxis des Tokens) | oauth | other (Token gehört einer anderen Praxis) | off."""
    m = mode(settings)
    if m == "token":
        if token_practice_id(db, settings) == current_practice_id():
            return "token"
        return "oauth" if settings.bexio_client_id and settings.bexio_client_secret else "other"
    return m


def connection(db: Database):
    return db.one("SELECT * FROM bexio_connections WHERE practice_id = ?", (current_practice_id(),))


def connected(db: Database, settings) -> bool:
    m = mode_here(db, settings)
    if m == "token":
        return True
    row = connection(db)
    return m == "oauth" and row is not None and bool(row["refresh_token_enc"])


def redirect_uri(settings) -> str:
    return f"{settings.base_url}/leitung/einstellungen/export/bexio/zurueck"


def start_oauth(db: Database, settings) -> str:
    """Neuer Zustand (gegen Fälschung) und Adresse der bexio-Anmeldung."""
    state = secrets.token_urlsafe(24)
    db.execute("INSERT INTO bexio_connections (practice_id, pending_state, pending_at, updated_at) VALUES (?,?,?,?)"
               " ON CONFLICT(practice_id) DO UPDATE SET pending_state = excluded.pending_state, pending_at = excluded.pending_at,"
               " updated_at = excluded.updated_at", (current_practice_id(), hashlib.sha256(state.encode()).hexdigest(), utcnow(), utcnow()))
    q = urlencode({"client_id": settings.bexio_client_id, "redirect_uri": redirect_uri(settings), "response_type": "code",
                   "scope": SCOPES, "state": state})
    return f"{settings.bexio_auth}/auth?{q}"


def check_state(db: Database, state: str | None) -> bool:
    row = connection(db)
    if row is None or not row["pending_state"] or not state or not row["pending_at"]:
        return False
    from datetime import datetime, timedelta, timezone

    fresh = datetime.fromisoformat(row["pending_at"]) > datetime.now(timezone.utc) - timedelta(minutes=15)
    ok = fresh and secrets.compare_digest(row["pending_state"], hashlib.sha256(state.encode()).hexdigest())
    db.execute("UPDATE bexio_connections SET pending_state = NULL, pending_at = NULL WHERE practice_id = ?", (current_practice_id(),))
    return ok


async def _token_request(settings, data: dict) -> dict:
    body = {"client_id": settings.bexio_client_id, "client_secret": settings.bexio_client_secret, **data}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.post(f"{settings.bexio_auth}/token", data=body, headers={"Accept": "application/json"}) as r:
                text = await r.text()
                if r.status != 200:
                    raise BexioError(_message(r.status, text))
                return json.loads(text)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise BexioError(f"bexio nicht erreichbar ({type(e).__name__})") from e


def _store_refresh(db: Database, settings, token: str | None) -> None:
    if not token:
        return
    db.execute("INSERT INTO bexio_connections (practice_id, refresh_token_enc, connected_at, connected_by, updated_at) VALUES (?,?,?,?,?)"
               " ON CONFLICT(practice_id) DO UPDATE SET refresh_token_enc = excluded.refresh_token_enc, updated_at = excluded.updated_at,"
               " connected_at = COALESCE(bexio_connections.connected_at, excluded.connected_at),"
               " connected_by = COALESCE(bexio_connections.connected_by, excluded.connected_by)",
               (current_practice_id(), crypto.encrypt(settings.secret_key, PURPOSE, token), utcnow(), current_user_id(), utcnow()))


async def finish_oauth(db: Database, settings, code: str) -> None:
    data = await _token_request(settings, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(settings)})
    _store_refresh(db, settings, data.get("refresh_token"))
    if data.get("access_token"):
        _TOKENS[current_practice_id()] = (data["access_token"], time.time() + float(data.get("expires_in") or 300) - 30)
    audit(db, "connect", "bexio", current_practice_id())


def disconnect(db: Database) -> None:
    db.execute("UPDATE bexio_connections SET refresh_token_enc = NULL, connected_at = NULL, connected_by = NULL, updated_at = ?"
               " WHERE practice_id = ?", (utcnow(), current_practice_id()))
    _TOKENS.pop(current_practice_id(), None)
    audit(db, "disconnect", "bexio", current_practice_id())


async def access_token(db: Database, settings) -> str:
    m = mode_here(db, settings)
    if m == "token":
        return settings.bexio_token
    if m != "oauth":
        raise BexioError("bexio ist für diese Praxis nicht eingerichtet")
    cached = _TOKENS.get(current_practice_id())
    if cached and cached[1] > time.time():
        return cached[0]
    row = connection(db)
    if row is None or not row["refresh_token_enc"]:
        raise BexioError("Noch nicht mit bexio verbunden")
    refresh = crypto.decrypt(settings.secret_key, PURPOSE, row["refresh_token_enc"])
    data = await _token_request(settings, {"grant_type": "refresh_token", "refresh_token": refresh})
    _store_refresh(db, settings, data.get("refresh_token"))       # bexio kann ein neues Refresh-Token geben
    _TOKENS[current_practice_id()] = (data["access_token"], time.time() + float(data.get("expires_in") or 300) - 30)
    return data["access_token"]


# --------------------------------------------------------------------------- API
def _message(status: int, text: str) -> str:
    try:
        msg = json.loads(text).get("message") or json.loads(text).get("error_description") or text
    except (ValueError, AttributeError):
        msg = text
    return f"bexio {status}: {str(msg)[:200]}"


class Client:
    def __init__(self, settings, token: str, session: aiohttp.ClientSession):
        self.base = settings.bexio_api
        self.headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        self.s = session

    async def _call(self, method: str, path: str, body: dict | None = None, *, retry: bool = True):
        try:
            async with self.s.request(method, self.base + path, json=body, headers=self.headers) as r:
                text = await r.text()
                if r.status == 429 and retry:
                    wait = min(60, int(r.headers.get("RateLimit-Reset", "5") or 5))
                    await asyncio.sleep(wait)
                    return await self._call(method, path, body, retry=False)
                if r.status >= 500:
                    raise Unclear(_message(r.status, text))
                if r.status >= 400:
                    raise BexioError(_message(r.status, text))
                return json.loads(text) if text else None
        except asyncio.TimeoutError as e:
            raise Unclear("Zeitüberschreitung") from e
        except aiohttp.ClientError as e:
            raise Unclear(f"Verbindung unterbrochen ({type(e).__name__})") from e

    async def accounts(self) -> dict[str, int]:
        """Kontonummer → bexio-ID (nur aktive Konten)."""
        rows = await self._call("GET", "/2.0/accounts?limit=2000") or []
        return {str(a.get("account_no")): int(a["id"]) for a in rows if a.get("is_active", True)}

    async def chf(self) -> int:
        rows = await self._call("GET", "/3.0/currencies") or []
        for c in rows:
            if (c.get("name") or "").upper() == "CHF":
                return int(c["id"])
        raise BexioError("Währung CHF in bexio nicht gefunden")

    async def book(self, e: Entry, debit_id: int, credit_id: int, currency_id: int) -> int:
        body = {"type": "manual_single_entry", "date": e.day, "reference_nr": e.doc[:80],
                "entries": [{"debit_account_id": debit_id, "credit_account_id": credit_id, "amount": round(e.amount, 2),
                             "description": e.text[:255], "currency_id": currency_id, "currency_factor": 1}]}
        res = await self._call("POST", "/3.0/accounting/manual_entries", body, retry=True)
        return int((res or {}).get("id") or 0)


# --------------------------------------------------------------------------- Übertragen
STABLE = "src:"          # Schlüssel ab 0.9.0: «src:» + Herkunft der Journalzeile (nur IDs, nie Namen oder Text)


def legacy_key(e: Entry) -> str:
    """Schlüssel bis 0.8.0 (mit Text – ändert sich beim Umbenennen). Nur noch zum Wiedererkennen alter Zeilen."""
    raw = f"{e.day}|{e.doc}|{e.debit}|{e.credit}|{e.amount:.2f}|{e.text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def entry_key(e: Entry) -> str:
    """Fester Schlüssel einer Journalzeile aus ihrer Herkunft (Entry.src) – unabhängig von Namen, Texten und Kontenplan."""
    return STABLE + e.src if e.src else legacy_key(e)


def bookings(db: Database, keys: list[str]) -> dict[str, dict]:
    if not keys:
        return {}
    out = {}
    for i in range(0, len(keys), 500):
        part = keys[i:i + 500]
        rows = db.query(f"SELECT * FROM bexio_bookings WHERE practice_id = ? AND entry_key IN ({','.join('?' * len(part))})",
                        (current_practice_id(), *part))
        for r in rows:
            out[r["entry_key"]] = dict(r)
    return out


def _legacy_rows(db: Database, entries: list[Entry]) -> list[dict]:
    """Zeilen mit altem Schlüssel (vor 0.9.0) an den Tagen dieser Einträge."""
    days = sorted({e.day for e in entries})
    out: list[dict] = []
    for i in range(0, len(days), 500):
        part = days[i:i + 500]
        out += [dict(r) for r in db.query(
            f"SELECT * FROM bexio_bookings WHERE practice_id = ? AND entry_key NOT LIKE 'src:%' AND day IN ({','.join('?' * len(part))})"
            " ORDER BY id", (current_practice_id(), *part))]
    return out


def resolve(db: Database, entries: list[Entry], *, adopt: bool = True) -> dict[str, dict]:
    """Bekannte Buchungen je Schlüssel (entry_key). Zeilen, die vor 0.9.0 gesendet wurden (alter Schlüssel mit Text), werden über
    Datum, Beleg, Konten und Betrag wiedererkannt – auch wenn sich der Text seither geändert hat (Firma umbenannt, Name korrigiert) –
    und bekommen den festen Schlüssel (adopt=True; jede alte Zeile nur für einen Eintrag). So wird nichts doppelt gesendet."""
    keys = [entry_key(e) for e in entries]
    known = bookings(db, keys)
    missing = [e for e, k in zip(entries, keys) if k not in known and e.src]
    if not missing:
        return known
    legacy = _legacy_rows(db, missing)
    if not legacy:
        return known
    used: set[int] = set()
    for e in missing:
        same = [r for r in legacy if r["id"] not in used and r["day"] == e.day and r["doc"] == e.doc and r["debit"] == e.debit
                and r["credit"] == e.credit and abs(float(r["amount_chf"]) - e.amount) < 0.005]
        if not same:
            continue
        row = next((r for r in same if r["entry_key"] == legacy_key(e)), same[0])      # gleicher Text zuerst
        used.add(row["id"])
        new_key = entry_key(e)
        if adopt:
            db.execute("UPDATE bexio_bookings SET entry_key = ?, updated_at = ? WHERE id = ? AND practice_id = ?",
                       (new_key, utcnow(), row["id"], current_practice_id()))
        known[new_key] = {**row, "entry_key": new_key}
    return known


def plan(db: Database, entries: list[Entry]) -> dict:
    """Was beim Senden passiert: neu / schon gesendet / abgelehnt (erneut senden) / unklar (erst prüfen)."""
    known = resolve(db, entries)
    out = {"new": 0, "sent": 0, "failed": 0, "unknown": 0}
    for e in entries:
        row = known.get(entry_key(e))
        out[row["status"] if row else "new"] += 1
    return out


def problems(db: Database) -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM bexio_bookings WHERE practice_id = ? AND status IN ('failed', 'unknown')"
                                      " ORDER BY day, doc LIMIT 200", (current_practice_id(),))]


def _record(db: Database, e: Entry, status: str, bexio_id: int | None = None, error: str | None = None) -> None:
    db.execute("INSERT INTO bexio_bookings (practice_id, entry_key, day, doc, debit, credit, amount_chf, text, status, bexio_id, error,"
               " created_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(practice_id, entry_key) DO UPDATE SET"
               " status = excluded.status, bexio_id = excluded.bexio_id, error = excluded.error, updated_at = excluded.updated_at",
               (current_practice_id(), entry_key(e), e.day, e.doc, e.debit, e.credit, round(e.amount, 2), e.text, status, bexio_id,
                (error or "")[:300] or None, current_user_id(), utcnow()))


async def check(db: Database, settings, needed: set[str], *, token: str | None = None) -> dict:
    """Verbindung prüfen: Konten lesen, fehlende Kontonummern melden. token: nur für den Verbindungstest der Box
    (check-connections, noch keine Praxis angelegt) – sonst immer der Zugang der aktuellen Praxis."""
    token = token or await access_token(db, settings)
    async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
        c = Client(settings, token, s)
        accounts = await c.accounts()
        await c.chf()
    return {"accounts": len(accounts), "missing": sorted(n for n in needed if n not in accounts)}


async def send(db: Database, settings, entries: list[Entry]) -> dict:
    """Neue und abgelehnte Zeilen senden. Gesendete und unklare Zeilen bleiben unberührt."""
    known = resolve(db, entries)
    todo = [e for e in entries if (known.get(entry_key(e)) or {}).get("status") in (None, "failed")]
    result = {"sent": 0, "failed": 0, "unknown": 0, "skipped": len(entries) - len(todo), "missing": []}
    if not todo:
        return result
    token = await access_token(db, settings)
    async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
        c = Client(settings, token, s)
        accounts = await c.accounts()
        chf = await c.chf()
        missing = sorted({n for e in todo for n in (e.debit, e.credit) if n not in accounts})
        result["missing"] = missing
        for e in todo:
            if e.debit not in accounts or e.credit not in accounts:
                _record(db, e, "failed", error=f"Konto fehlt in bexio: {', '.join(n for n in (e.debit, e.credit) if n not in accounts)}")
                result["failed"] += 1
                continue
            try:
                bid = await c.book(e, accounts[e.debit], accounts[e.credit], chf)
                _record(db, e, "sent", bexio_id=bid or None)
                result["sent"] += 1
            except BexioError as err:
                _record(db, e, "failed", error=str(err))
                result["failed"] += 1
                if "401" in str(err) or "403" in str(err):
                    break                               # Zugang abgelaufen: nicht weiter versuchen
            except Unclear as err:
                _record(db, e, "unknown", error=str(err))
                result["unknown"] += 1
                break                                   # bei Störung aufhören, nicht blind weiter senden
    audit(db, "export", "bexio", None, None, {k: v for k, v in result.items() if k != "missing"})
    return result


def mark(db: Database, booking_id: int, status: str) -> bool:
    """Unklare Zeile nach Prüfung in bexio: «sent» (ist dort) oder «failed» (fehlt dort → neu senden)."""
    if status not in ("sent", "failed"):
        return False
    row = db.one("SELECT * FROM bexio_bookings WHERE id = ? AND practice_id = ?", (booking_id, current_practice_id()))
    if row is None or row["status"] == "sent":
        return False
    db.execute("UPDATE bexio_bookings SET status = ?, error = CASE WHEN ? = 'sent' THEN NULL ELSE error END, updated_at = ? WHERE id = ?",
               (status, status, utcnow(), booking_id))
    audit(db, "mark", "bexio_booking", booking_id, None, {"status": status})
    return True
