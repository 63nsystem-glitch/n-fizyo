"""Passwörter, Tokens und signierte Links."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any

# Passwörter: scrypt mit N = 2**17 (128 MiB Speicher, ca. 0,4 s pro Prüfung). Gespeichert als
#   scrypt$ln=17,r=8,p=1$<salz>$<hash>
# Ältere Hashes (bis 0.8.0) haben die Form scrypt$<salz>$<hash> mit N = 2**14; sie gelten weiter und werden beim
# nächsten erfolgreichen Anmelden still mit den neuen Werten ersetzt (needs_rehash).
SCRYPT_LOG_N = 17
SCRYPT_R = 8
SCRYPT_P = 1
_LEGACY = (14, 8, 1)
# Höchstens zwei Berechnungen gleichzeitig: viele Anmeldeversuche auf einmal dürfen den Speicher nicht füllen.
_HASHING = threading.BoundedSemaphore(2)


def _scrypt(password: str, salt: bytes, ln: int, r: int, p: int) -> bytes:
    n = 2 ** ln
    maxmem = 128 * r * (n + p + 2) + 2 ** 20   # Speicherbedarf von scrypt + Reserve (Standard wäre nur 32 MiB)
    with _HASHING:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=maxmem)


def _params(stored: str) -> tuple[tuple[int, int, int], bytes, bytes] | None:
    parts = stored.split("$")
    try:
        if len(parts) == 3 and parts[0] == "scrypt":
            return _LEGACY, base64.b64decode(parts[1]), base64.b64decode(parts[2])
        if len(parts) == 4 and parts[0] == "scrypt":
            kv = dict(item.split("=", 1) for item in parts[1].split(","))
            params = (int(kv["ln"]), int(kv["r"]), int(kv["p"]))
            if not (10 <= params[0] <= 20 and 1 <= params[1] <= 32 and 1 <= params[2] <= 4):
                return None
            return params, base64.b64decode(parts[2]), base64.b64decode(parts[3])
    except (ValueError, KeyError):
        return None
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    ln, r, p = SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P
    digest = _scrypt(password, salt, ln, r, p)
    return f"scrypt$ln={ln},r={r},p={p}$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    parsed = _params(stored or "")
    if parsed is None:
        return False
    (ln, r, p), salt, expected = parsed
    return hmac.compare_digest(_scrypt(password, salt, ln, r, p), expected)


def needs_rehash(stored: str) -> bool:
    """True, wenn der Hash mit schwächeren (älteren) Werten erstellt wurde."""
    parsed = _params(stored or "")
    return parsed is None or parsed[0] != (SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P)


_DUMMY: dict[tuple[int, int, int], str] = {}


def dummy_verify(password: str) -> None:
    """Gleich viel Arbeit wie eine echte Prüfung – für unbekannte E-Mail-Adressen (die Antwortzeit verrät nichts)."""
    key = (SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P)
    if key not in _DUMMY:
        _DUMMY[key] = hash_password(new_token(16))
    verify_password(password, _DUMMY[key])


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_code(digits: int = 6) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(digits))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: bytes, purpose: str, payload: dict[str, Any], ttl_seconds: int) -> str:
    """Signierter, nicht erratbarer Link-Token (HMAC-SHA256) mit Ablaufzeit."""
    body = dict(payload, _p=purpose, _e=int(time.time()) + ttl_seconds)
    raw = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(secret, raw.encode(), hashlib.sha256).digest())
    return f"{raw}.{sig}"


def unsign(secret: bytes, purpose: str, token: str) -> dict[str, Any] | None:
    try:
        raw, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = _b64(hmac.new(secret, raw.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        body = json.loads(_unb64(raw))
    except (ValueError, json.JSONDecodeError):
        return None
    if body.get("_p") != purpose or int(body.get("_e", 0)) < int(time.time()):
        return None
    body.pop("_p", None)
    body.pop("_e", None)
    return body
