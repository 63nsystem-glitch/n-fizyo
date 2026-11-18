"""N-Fizyo Servis – Verbindung Laptop → Box über das Servicekabel (ohne Oberfläche, auch für Tests).

Ablauf jeder Anfrage: GET /hello (Einmal-Code) → POST /<aktion> mit Signatur über {action, nonce, body_sha256, args}.
Der private Service-Schlüssel liegt nur auf dem Laptop (Standard: %APPDATA%\\NSystem\\n-fizyo-service-key.pem), nie im
Projektordner, nie im Git, nie im Chat. Regeln: docs/SERVIS_KABLOSU.md
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nfizyo.agent import servicelink, signing  # noqa: E402


class ServiceError(Exception):
    pass


def default_key_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "NSystem" / "n-fizyo-service-key.pem"


def create_key(path: Path) -> str:
    """Neuen Service-Schlüssel anlegen (nie überschreiben). → öffentlicher Schlüssel (Base64) für service-port-setup.sh."""
    path = Path(path)
    if path.exists():
        raise ServiceError(f"Anahtar zaten var: {path}")
    if ROOT in path.resolve().parents:
        raise ServiceError("Anahtar proje klasörüne konamaz (Git!)")
    path.parent.mkdir(parents=True, exist_ok=True)
    pem, pub = signing.new_signing_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return pub


def load_key(path: Path) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise ServiceError(f"Anahtar okunamadı: {path} ({exc.strerror})") from exc


def public_key(path: Path) -> str:
    return signing.public_of(load_key(path))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class BoxClient:
    def __init__(self, private_pem: bytes, host: str = servicelink.BOX_ADDRESS, port: int = servicelink.PORT, timeout: float = 15):
        self.pem, self.host, self.port, self.timeout = private_pem, host, port, timeout

    def _conn(self, timeout: float | None = None) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)

    @staticmethod
    def _answer(resp: http.client.HTTPResponse) -> dict:
        raw = resp.read()
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        if resp.status != 200:
            raise ServiceError(str(data.get("error") or f"HTTP {resp.status}"))
        return data

    def hello(self) -> dict:
        try:
            c = self._conn(5)
            c.request("GET", "/hello")
            return self._answer(c.getresponse())
        except OSError as exc:
            raise ServiceError("Kutuya ulaşılamıyor – kablo takılı mı, servis girişi doğru mu?") from exc

    def call(self, action: str, args: dict | None = None, body: bytes = b"", timeout: float | None = None) -> dict:
        nonce = self.hello()["nonce"]
        data = servicelink.payload(action, nonce, servicelink.sha256_bytes(body), args)
        try:
            c = self._conn(timeout)
            c.request("POST", f"/{action}", body=body, headers={**servicelink.headers(self.pem, data), "Content-Length": str(len(body))})
            return self._answer(c.getresponse())
        except OSError as exc:
            raise ServiceError(f"Bağlantı koptu ({action})") from exc

    def status(self) -> dict:
        return self.call("status")

    def progress(self) -> dict:
        return self.call("progress")

    def update(self) -> dict:
        return self.call("update")

    def rollback(self) -> dict:
        return self.call("rollback")

    def backup(self) -> dict:
        return self.call("backup")

    def upload(self, manifest_path: Path) -> dict:
        """Manifest (n-fizyo-<sürüm>.manifest.json) + yanındaki arşiv → kutu kontrol eder ve bekletir. → {manifest, diff}."""
        manifest_path = Path(manifest_path)
        try:
            doc = json.loads(manifest_path.read_text(encoding="utf-8"))
            file = doc["manifest"]["file"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ServiceError("Manifest dosyası okunamadı") from exc
        archive = manifest_path.parent / Path(str(file)).name
        if not archive.exists():
            raise ServiceError(f"Arşiv bulunamadı: {archive.name} (manifest ile aynı klasörde olmalı)")
        size = archive.stat().st_size
        if size > servicelink.MAX_PACKAGE:
            raise ServiceError("Paket çok büyük")
        nonce = self.hello()["nonce"]
        data = servicelink.payload("upload", nonce, sha256_file(archive), {"manifest": doc})
        try:
            c = self._conn(600)
            with open(archive, "rb") as f:
                c.request("POST", "/upload", body=f, headers={**servicelink.headers(self.pem, data), "Content-Length": str(size)})
                return self._answer(c.getresponse())
        except OSError as exc:
            raise ServiceError("Yükleme sırasında bağlantı koptu") from exc
