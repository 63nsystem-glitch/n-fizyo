"""Signaturen zwischen Box und NSystem-Übersicht.

Meldung Box → NSystem: HMAC-SHA256 mit dem Gerätegeheimnis der Box über «Zeitstempel\\nInhalt».
Befehl NSystem → Box: Ed25519-Signatur über den Befehl in kanonischer Form (JSON, Schlüssel sortiert).
Weitere Inhalte (Wartungshinweise, Antworten auf Support-Anfragen, Release-Manifeste): sign_payload() über {"kind", "data"} –
durch «kind» kann eine solche Signatur nie als Befehl (oder als andere Art) gelten.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

MAX_SKEW = 300  # Sekunden
COMMAND_FIELDS = ("id", "box_id", "action", "args", "issued_by", "issued_at", "expires_at")


def report_signature(box_key: str, timestamp: str, body: bytes) -> str:
    return hmac.new(box_key.encode(), timestamp.encode() + b"\n" + body, hashlib.sha256).hexdigest()


def check_report(box_key: str, timestamp: str, body: bytes, signature: str, now: float | None = None) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now or time.time()) - ts) > MAX_SKEW:
        return False
    return hmac.compare_digest(report_signature(box_key, timestamp, body), signature or "")


def canonical(command: dict) -> bytes:
    return json.dumps({k: command.get(k) for k in COMMAND_FIELDS}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


# --------------------------------------------------------------------------- Ed25519
def new_signing_key() -> tuple[bytes, str]:
    """(privater Schlüssel als PEM, öffentlicher Schlüssel als Base64) – nur für die NSystem-Übersicht."""
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return pem, base64.b64encode(pub).decode()


def sign_command(private_pem: bytes, command: dict) -> str:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(private_pem, password=None)
    return base64.b64encode(key.sign(canonical(command))).decode()


def verify_command(public_b64: str, command: dict, *, box_id: str, now: float | None = None) -> str | None:
    """None = gültig, sonst Grund der Ablehnung."""
    if not public_b64:
        return "no_public_key"
    if command.get("box_id") != box_id:
        return "wrong_box"
    try:
        expires = float(command.get("expires_at") or 0)
    except (TypeError, ValueError):
        return "bad_expiry"
    if expires < (now or time.time()):
        return "expired"
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64)).verify(base64.b64decode(command.get("signature") or ""),
                                                                               canonical(command))
    except (InvalidSignature, ValueError):
        return "bad_signature"
    return None


def canonical_payload(kind: str, data: dict) -> bytes:
    return json.dumps({"kind": kind, "data": data}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_payload(private_pem: bytes, kind: str, data: dict) -> str:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(private_pem, password=None)
    return base64.b64encode(key.sign(canonical_payload(kind, data))).decode()


def verify_payload(public_b64: str, kind: str, data: dict, signature: str) -> bool:
    if not public_b64 or not signature:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64)).verify(base64.b64decode(signature), canonical_payload(kind, data))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def public_of(private_pem: bytes) -> str:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(private_pem, password=None)
    return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
