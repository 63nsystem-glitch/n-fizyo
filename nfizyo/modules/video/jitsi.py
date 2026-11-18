"""Schlüssel (JWT, HS256) für den eigenen Jitsi-Server – ohne fremde Bibliothek.

Der Server (deploy/jitsi) prüft: Herausgeber (iss = JWT_APP_ID), Empfänger (aud = jitsi), Raum, Gültigkeit (nbf/exp).
moderator=True nur für die Therapeutin / den Therapeuten.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import quote, urlparse


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def token(secret: str, app_id: str, server_url: str, room: str, name: str, *, moderator: bool, nbf: int, exp: int) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"aud": "jitsi", "iss": app_id, "sub": urlparse(server_url).hostname or "*", "room": room, "nbf": nbf, "exp": exp,
               "context": {"user": {"name": name, "moderator": moderator}}}
    signing = _b64(json.dumps(header, separators=(",", ":")).encode()) + "." + _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), signing.encode(), hashlib.sha256).digest()
    return signing + "." + _b64(sig)


def decode(secret: str, jwt: str) -> dict | None:
    """Nur für Tests und Prüfungen: Signatur prüfen und Inhalt zurückgeben."""
    try:
        head, body, sig = jwt.split(".")
    except ValueError:
        return None
    expected = _b64(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return None
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def room_url(server_url: str, room: str, jwt: str, display_name: str, lang: str) -> str:
    """Link in den Raum. Anzeigename und Sprache über den URL-Anhang (#), der nicht an den Server geht."""
    lang = {"bks": "hr", "gsw": "de"}.get(lang, lang)
    return (f"{server_url}/{room}?jwt={jwt}#userInfo.displayName=%22{quote(display_name)}%22"
            f"&config.defaultLanguage=%22{lang}%22&config.prejoinConfig.enabled=true")
