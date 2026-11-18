"""Servicekabel: gemeinsame Regeln für die Box (agent/service.py) und das Service-Programm von NSystem (tools/servis/).

NSystem verbindet einen eigenen Laptop mit einem Netzwerkkabel direkt mit dem SERVICE-ANSCHLUSS der Box (eigene
Netzwerkkarte, nicht das Praxisnetz). Die Box hat dort die feste Adresse 169.254.77.1 (link-local); Windows gibt sich ohne
DHCP selbst eine 169.254.x.y-Adresse – am Laptop muss nichts eingestellt werden.

Jede Anfrage (ausser /hello) ist mit dem SERVICE-Schlüssel von NSystem signiert (Ed25519, privater Teil nur auf dem Laptop,
öffentlicher Teil in NFIZYO_SERVICE_PUBKEY). Signiert wird {action, nonce, body_sha256, args}:
  - nonce: von der Box (/hello), einmalig, 5 Minuten gültig → keine Wiederholung, keine Uhr am Laptop nötig
  - body_sha256: Prüfsumme des Inhalts (beim Paket: das ganze Archiv)
Ein neues Programm wird zusätzlich wie immer geprüft: Manifest mit dem RELEASE-Schlüssel signiert, Prüfsumme, nur neuer.
"""
from __future__ import annotations

import base64
import hashlib
import json

from . import signing

KIND = "service-request"
BOX_ADDRESS = "169.254.77.1"
PORT = 8780
ACTIONS = ("status", "upload", "update", "progress", "rollback", "backup")
HEADER_REQUEST = "X-NFZ-Request"
HEADER_SIGNATURE = "X-NFZ-Signature"
NONCE_TTL = 300                     # Sekunden
MAX_PACKAGE = 400 * 1024 * 1024     # Programmpaket (tar.gz)
MAX_BODY = 64 * 1024                # alle anderen Anfragen


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def payload(action: str, nonce: str, body_sha256: str, args: dict | None = None) -> dict:
    return {"action": action, "nonce": nonce, "body_sha256": body_sha256, "args": args or {}}


def encode(data: dict) -> str:
    return base64.b64encode(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).decode()


def decode(text: str) -> dict:
    data = json.loads(base64.b64decode(text.encode(), validate=True))
    if not isinstance(data, dict):
        raise ValueError("payload")
    return data


def sign(private_pem: bytes, data: dict) -> str:
    return signing.sign_payload(private_pem, KIND, data)


def verify(public_b64: str, data: dict, signature: str) -> bool:
    return signing.verify_payload(public_b64, KIND, data, signature)


def headers(private_pem: bytes, data: dict) -> dict[str, str]:
    return {HEADER_REQUEST: encode(data), HEADER_SIGNATURE: sign(private_pem, data)}
