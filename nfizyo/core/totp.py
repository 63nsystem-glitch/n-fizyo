"""Zwei-Schritt-Anmeldung: 6-stellige Codes aus einer Authenticator-App (TOTP, RFC 6238) und Wiederherstellungscodes.

Funktioniert mit jeder üblichen App (Microsoft/Google Authenticator, 2FAS, Aegis, 1Password …), ohne Internet,
ohne SMS-Kosten. Ein Code gilt 30 Sekunden; ±1 Zeitfenster wird akzeptiert (Uhr am Telefon leicht verstellt).
Derselbe Code kann nicht zweimal benutzt werden (letztes Zeitfenster wird gespeichert).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6
WINDOW = 1
RECOVERY_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # ohne 0/o, 1/l/i


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP)


def verify(secret: str, code: str, last_step: int | None = None, now: float | None = None) -> int | None:
    """Gibt das benutzte Zeitfenster zurück (zum Speichern) oder None. Schon benutzte Fenster zählen nicht."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != DIGITS:
        return None
    step = current_step(now)
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        if last_step is not None and candidate <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def otpauth_uri(secret: str, account: str, issuer: str = "N-Fizyo") -> str:
    label = quote(f"{issuer}:{account}", safe="@:")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def pretty_secret(secret: str) -> str:
    """Zum Abtippen, falls die Kamera nicht geht: in Vierergruppen."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


# --------------------------------------------------------------------------- Wiederherstellungscodes
def new_recovery_codes(n: int = RECOVERY_COUNT) -> list[str]:
    def one() -> str:
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        return f"{raw[:5]}-{raw[5:]}"

    return [one() for _ in range(n)]


def _norm_recovery(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def hash_recovery(code: str) -> str:
    return hashlib.sha256(("nfizyo-recovery:" + _norm_recovery(code)).encode()).hexdigest()


def use_recovery(stored_hashes: list[str], code: str) -> list[str] | None:
    """Passt der Code, kommt die Liste ohne ihn zurück (jeder Code gilt nur einmal), sonst None."""
    h = hash_recovery(code)
    for i, stored in enumerate(stored_hashes):
        if hmac.compare_digest(stored, h):
            return stored_hashes[:i] + stored_hashes[i + 1:]
    return None


def looks_like_recovery(code: str) -> bool:
    return len(_norm_recovery(code)) == 10 and not _norm_recovery(code).isdigit()
