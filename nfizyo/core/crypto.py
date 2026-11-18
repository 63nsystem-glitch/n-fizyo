"""Verschlüsselung kleiner Geheimnisse in der Datenbank (z. B. Schlüssel der Zwei-Schritt-Anmeldung).

AES-256-GCM, Schlüssel abgeleitet aus NFIZYO_SECRET_KEY (HKDF, je Zweck ein eigener Schlüssel).
Wer nur die Datenbank hat (z. B. eine gestohlene Sicherung), kann die Geheimnisse nicht lesen.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PREFIX = "v1:"


def _key(secret_key: bytes, purpose: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"n-fizyo", info=purpose.encode()).derive(secret_key)


def derive(secret_key: bytes, purpose: str) -> bytes:
    """Eigener Schlüssel für einen Zweck (z. B. Signatur der Aufträge App → Runner)."""
    return _key(secret_key, purpose)


def encrypt(secret_key: bytes, purpose: str, plaintext: str) -> str:
    nonce = os.urandom(12)
    data = AESGCM(_key(secret_key, purpose)).encrypt(nonce, plaintext.encode("utf-8"), purpose.encode())
    return PREFIX + base64.b64encode(nonce + data).decode()


def decrypt(secret_key: bytes, purpose: str, token: str) -> str:
    if not token.startswith(PREFIX):
        raise ValueError("Unbekanntes Format")
    raw = base64.b64decode(token[len(PREFIX):])
    return AESGCM(_key(secret_key, purpose)).decrypt(raw[:12], raw[12:], purpose.encode()).decode("utf-8")
