"""Zwei-Schritt-Anmeldung für Praxiskonten: einrichten, prüfen, Wiederherstellungscodes, Zurücksetzen durch die Leitung.

Ablauf beim Anmelden:
  1. E-Mail + Passwort stimmen → Anmeldung in Stufe «code» (App-Code fehlt) bzw. «setup» (noch nicht eingerichtet).
  2. Erst nach gültigem Code (oder Einrichtung) wird daraus eine normale Anmeldung (neues Sitzungs-Token).
In Stufe «code»/«setup» ist man NICHT angemeldet: keine Seite der Praxis ist erreichbar.
"""
from __future__ import annotations

import json

from . import crypto, totp
from .audit import audit
from .db import Database, utcnow
from .tenant import practice_scope

PURPOSE = "totp"


def enabled(user) -> bool:
    return bool(user["totp_secret"]) and bool(user["totp_enabled_at"])


def recovery_left(user) -> int:
    return len(json.loads(user["recovery_codes"] or "[]"))


def begin_setup(db: Database, secret_key: bytes, user_id: int) -> str:
    """Neuen Schlüssel erzeugen (noch nicht aktiv, erst nach Bestätigung mit einem Code)."""
    secret = totp.new_secret()
    db.execute("UPDATE users SET totp_pending = ? WHERE id = ?", (crypto.encrypt(secret_key, PURPOSE, secret), user_id))
    return secret


def pending_secret(secret_key: bytes, user) -> str | None:
    if not user["totp_pending"]:
        return None
    try:
        return crypto.decrypt(secret_key, PURPOSE, user["totp_pending"])
    except Exception:  # noqa: BLE001 - anderer Schlüssel / beschädigt → neu einrichten
        return None


def confirm_setup(db: Database, secret_key: bytes, user, code: str) -> list[str] | None:
    """Code aus der App bestätigt die Einrichtung. Gibt die Wiederherstellungscodes zurück (einmalig anzeigen)."""
    secret = pending_secret(secret_key, user)
    if not secret:
        return None
    step = totp.verify(secret, code)
    if step is None:
        return None
    codes = totp.new_recovery_codes()
    db.execute(
        "UPDATE users SET totp_secret = ?, totp_pending = NULL, totp_enabled_at = ?, totp_last_step = ?, recovery_codes = ? WHERE id = ?",
        (crypto.encrypt(secret_key, PURPOSE, secret), utcnow(), step, json.dumps([totp.hash_recovery(c) for c in codes]), user["id"]),
    )
    with practice_scope(user["practice_id"], user["id"]):
        audit(db, "mfa_enabled", "user", user["id"])
    return codes


def check(db: Database, secret_key: bytes, user, code: str) -> str | None:
    """Prüft einen App-Code oder Wiederherstellungscode. Rückgabe: "totp" | "recovery" | None."""
    if not enabled(user):
        return None
    if totp.looks_like_recovery(code):
        left = totp.use_recovery(json.loads(user["recovery_codes"] or "[]"), code)
        if left is None:
            return None
        db.execute("UPDATE users SET recovery_codes = ? WHERE id = ?", (json.dumps(left), user["id"]))
        with practice_scope(user["practice_id"], user["id"]):
            audit(db, "mfa_recovery_used", "user", user["id"], None, {"left": len(left)})
        return "recovery"
    try:
        secret = crypto.decrypt(secret_key, PURPOSE, user["totp_secret"])
    except Exception:  # noqa: BLE001
        return None
    step = totp.verify(secret, code, user["totp_last_step"])
    if step is None:
        return None
    db.execute("UPDATE users SET totp_last_step = ? WHERE id = ?", (step, user["id"]))
    return "totp"


def new_recovery_codes(db: Database, user) -> list[str]:
    codes = totp.new_recovery_codes()
    db.execute("UPDATE users SET recovery_codes = ? WHERE id = ?", (json.dumps([totp.hash_recovery(c) for c in codes]), user["id"]))
    with practice_scope(user["practice_id"], user["id"]):
        audit(db, "mfa_recovery_new", "user", user["id"])
    return codes


def reset(db: Database, user_id: int) -> None:
    """Leitung: Telefon verloren → Zwei-Schritt-Anmeldung zurücksetzen. Die Person richtet sie beim nächsten Anmelden neu ein.
    Alle Anmeldungen der Person werden beendet. (Aufrufer prüft die Praxis.)"""
    db.execute("UPDATE users SET totp_secret = NULL, totp_pending = NULL, totp_enabled_at = NULL, totp_last_step = NULL,"
               " recovery_codes = NULL WHERE id = ?", (user_id,))
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
