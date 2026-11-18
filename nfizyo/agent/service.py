"""Servicekabel der Box (läuft als root, systemd «n-fizyo-service», eingerichtet mit deploy/service-port-setup.sh).

NSystem steckt einen eigenen Laptop per Netzwerkkabel am SERVICE-ANSCHLUSS der Box ein (eigene Netzwerkkarte, nicht das
Praxisnetz) und spielt mit dem Service-Programm (tools/servis/) ein neues Programm ein – ohne Internet, ohne Panel.

- Hört NUR auf 169.254.77.1:8780 (link-local, eigene Karte). Die Firewall lässt auf dieser Karte nur diesen Port zu.
- /hello (ohne Signatur): Box-ID, Version, Einmal-Code (nonce). Keine Praxis- oder Patientendaten.
- Alles andere: signiert mit dem Service-Schlüssel von NSystem (NFIZYO_SERVICE_PUBKEY), nonce einmalig, Prüfsumme des
  Inhalts passt (agent/servicelink.py). Ohne öffentlichen Schlüssel ist alles gesperrt.
- Feste Liste: status · upload (Paket) · update · progress · rollback · backup. Keine Dateien lesen, keine Datenbank, keine Shell.
- Neues Programm: dieselben Prüfungen wie «Neue Version» vom Panel (Manifest mit Release-Schlüssel signiert, Prüfsumme,
  nur neuer als installiert) und derselbe Ablauf (update.sh: Sicherung, Umschalten, bei Fehler automatisch zurück).
- Jede Aktion und jede abgewiesene Anfrage → Protokoll der Leitung (Einstellungen › NSystem), wie beim Supportzugang.

Aufruf: python -m nfizyo.agent.service --env /etc/n-fizyo/n-fizyo.env
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..core import nsystem
from ..core.config import load_settings
from . import release, runner, servicelink, support

STATE_DIR = Path(os.environ.get("NFIZYO_SERVICE_STATE", "/var/lib/n-fizyo-service"))
LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16")
JOB_LOG_LIMIT = 6000


def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    """Wie runner._run, aber mit NFIZYO_FROM_SERVICE=1: update.sh startet dann den Runner neu (neuer Code), nicht uns."""
    try:
        env = dict(os.environ, NFIZYO_FROM_SERVICE="1")
        env.pop("NFIZYO_FROM_RUNNER", None)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env)
        out = (p.stdout + ("\n" + p.stderr if p.stderr else "")).strip()
        return p.returncode, out[-JOB_LOG_LIMIT:]
    except subprocess.TimeoutExpired:
        return 124, "Zeitlimit überschritten"
    except FileNotFoundError as exc:
        return 127, str(exc)


class Service:
    """Zustand: Einmal-Codes, wartendes Paket, laufende Aufgabe (immer nur eine)."""

    def __init__(self, settings, state_dir: Path = STATE_DIR, run=None, events=True):
        self.settings = settings
        self.state_dir = state_dir
        self.run = run or _run
        self.events = events
        self.lock = threading.Lock()
        self.nonces: dict[str, float] = {}
        self.job: dict = {"state": "idle"}
        self.thread: threading.Thread | None = None
        self.restart_self = False
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (state_dir / "pending").mkdir(mode=0o700, exist_ok=True)
        # Letzte Aufgabe nach einem Neustart (nach dem Update lädt sich der Dienst neu) – der Laptop sieht weiter das Ergebnis
        try:
            last = json.loads((state_dir / "job.json").read_text())
        except (OSError, ValueError):
            last = None
        if isinstance(last, dict) and last.get("state") in ("done", "failed", "running"):
            if last["state"] == "running":                  # Dienst wurde mitten in der Aufgabe beendet (Stromausfall …)
                last.update(state="unknown", log=str(last.get("log") or "") + "\nDienst wurde während der Aufgabe neu gestartet.")
            self.job = last

    # ------------------------------------------------------------------ Einmal-Codes
    def new_nonce(self) -> str:
        now = time.time()
        with self.lock:
            self.nonces = {n: t for n, t in self.nonces.items() if t > now}
            if len(self.nonces) > 200:                       # niemand braucht so viele offene Codes
                self.nonces.clear()
            nonce = secrets.token_urlsafe(24)
            self.nonces[nonce] = now + servicelink.NONCE_TTL
        return nonce

    def take_nonce(self, nonce: str) -> bool:
        with self.lock:
            expires = self.nonces.pop(nonce, 0)
        return expires > time.time()

    # ------------------------------------------------------------------ Protokoll der Leitung
    def log(self, kind: str, detail: dict) -> None:
        if not self.events:
            return
        try:
            support.emit(nsystem.runner_paths(self.settings)["events"], kind, detail, chown=runner._chown)
        except OSError:
            pass

    # ------------------------------------------------------------------ Aktionen
    def hello(self) -> dict:
        return {"name": "n-fizyo", "box_id": self.settings.central_box_id or "", "version": runner.installed_version() or "",
                "nonce": self.new_nonce(), "ready": bool(self.settings.service_pubkey)}

    def status(self) -> dict:
        app = runner.APP_DIR / "app"
        try:
            modules = release.module_versions(app)
        except OSError:
            modules = {}
        return {"version": runner.installed_version(), "modules": modules, "health": _health(), "services": runner.services(),
                "gpu": runner.gpu(), "backup_age_h": runner.backup_age_hours(), "backup": runner.backup_status(),
                "disk_free_gb": _disk_free(self.settings.data_dir), "pending": self.pending_manifest(), "job": self.job_view()}

    def pending_manifest(self) -> dict | None:
        try:
            m = json.loads((self.state_dir / "pending" / "manifest.json").read_text())
        except (OSError, ValueError):
            return None
        return m if isinstance(m, dict) and (self.state_dir / "pending" / str(m.get("file") or "-")).exists() else None

    def accept_package(self, tmp: Path, body_sha: str, manifest_doc) -> tuple[int, dict]:
        """Hochgeladenes Archiv prüfen und als wartendes Paket ablegen. → (HTTP-Status, Antwort)."""
        pub = self.settings.release_pubkey
        if not pub:
            return 409, {"error": "NFIZYO_RELEASE_PUBKEY fehlt – nur signierte Versionen"}
        try:
            manifest = release.verify_manifest(pub, manifest_doc)
        except release.ReleaseError as exc:
            return 400, {"error": f"Manifest abgelehnt: {exc}"}
        if manifest["sha256"] != body_sha:
            return 400, {"error": "Prüfsumme des Pakets passt nicht zum Manifest"}
        refused = runner.downgrade_reason(manifest["version"])
        if refused:
            return 409, {"error": refused}
        with self.lock:
            if self.job.get("state") == "running":
                return 409, {"error": "Es läuft gerade eine Aufgabe"}
            folder = self.state_dir / "pending"
            for old in folder.iterdir():
                old.unlink(missing_ok=True)
            tmp.replace(folder / manifest["file"])
            (folder / "manifest.json").write_text(json.dumps(manifest))
        self.log("service_action", {"action": "upload", "result": "ok", "version": manifest["version"]})
        return 200, {"ok": True, "manifest": manifest, "diff": self.diff(manifest)}

    def diff(self, manifest: dict) -> dict:
        """Was ändert sich: Programm, Module (alt → neu), Datenbank (neue letzte Migration je Besitzer)."""
        app = runner.APP_DIR / "app"
        try:
            old_mods, old_mig = release.module_versions(app), release.migration_info(app)
        except OSError:
            old_mods, old_mig = {}, {}
        new_mods, new_mig = manifest.get("modules") or {}, manifest.get("migrations") or {}
        modules = {k: [old_mods.get(k), new_mods.get(k)] for k in sorted(set(old_mods) | set(new_mods)) if old_mods.get(k) != new_mods.get(k)}
        db = sorted(k for k in set(new_mig) if new_mig.get(k) != old_mig.get(k))
        return {"from": runner.installed_version(), "to": manifest.get("version"), "modules": modules, "db_changes": db,
                "changelog": manifest.get("changelog") or []}

    def start(self, action: str) -> tuple[int, dict]:
        with self.lock:
            if self.job.get("state") == "running":
                return 409, {"error": "Es läuft gerade eine Aufgabe"}
            if action == "update" and not self.pending_manifest():
                return 409, {"error": "Kein geprüftes Paket vorhanden – zuerst hochladen"}
            self.job = {"state": "running", "action": action, "started_at": time.time(), "log": ""}
            self._save_job()
            self.thread = threading.Thread(target=self._work, args=(action,), daemon=True)
            self.thread.start()
        self.log("service_action", {"action": action, "result": "started"})
        return 200, {"ok": True, "job": self.job_view()}

    def _work(self, action: str) -> None:
        try:
            if action == "update":
                code, text = self._update()
            elif action == "rollback":
                code, text = self.run(["bash", str(runner.APP_DIR / "app/deploy/update.sh"), "--rollback"], runner.TIMEOUTS["rollback"])
            else:
                code, text = self.run(["/usr/local/sbin/n-fizyo-backup"], runner.TIMEOUTS["backup_now"])
        except Exception as exc:  # noqa: BLE001 - Aufgabe darf den Dienst nie anhalten
            code, text = 1, f"Fehler: {type(exc).__name__}"
        with self.lock:
            self.job.update(state="done" if code == 0 else "failed", finished_at=time.time(), code=code, log=text[-JOB_LOG_LIMIT:])
            self._save_job()
            if code == 0 and action in ("update", "rollback"):
                self.restart_self = True                   # neuen Code laden (siehe main)
        self.log("service_action", {"action": action, "result": "ok" if code == 0 else "failed",
                                    "version": runner.installed_version() or ""})

    def _update(self) -> tuple[int, str]:
        manifest = self.pending_manifest()
        if not manifest:
            return 1, "Kein Paket"
        work = Path(tempfile.mkdtemp(prefix="n-fizyo-service-"))
        try:
            code, text = runner.install_archive(work, self.state_dir / "pending" / manifest["file"], manifest, run=self.run)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if code == 0:
            for f in (self.state_dir / "pending").iterdir():
                f.unlink(missing_ok=True)
        return code, text

    def _save_job(self) -> None:
        """Nur unter self.lock aufrufen."""
        try:
            tmp = self.state_dir / ".job.json.tmp"
            tmp.write_text(json.dumps(self.job))
            tmp.replace(self.state_dir / "job.json")
        except OSError:
            pass

    def job_view(self) -> dict:
        with self.lock:
            return dict(self.job)


def _health() -> dict:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=3) as r:
            data = json.loads(r.read(4096))
        return {"ok": True, "version": data.get("version") if isinstance(data, dict) else None}
    except (OSError, ValueError):
        return {"ok": False}


def _disk_free(path: Path) -> float | None:
    try:
        return round(shutil.disk_usage(path).free / 1e9, 1)
    except OSError:
        return None


def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "n-fizyo-service"
        sys_version = ""

        def log_message(self, *args):     # kein Zugriffsprotokoll mit Adressen auf der Konsole
            pass

        def _send(self, code: int, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _deny(self, code: int, reason: str, action: str = "") -> None:
            service.log("service_denied", {"why": reason, "action": action[:20]})
            self._send(code, {"error": reason})

        def do_GET(self):
            if self.path != "/hello":
                return self._send(404, {"error": "not_found"})
            self._send(200, service.hello())

        def do_POST(self):
            action = self.path.strip("/")
            if action not in servicelink.ACTIONS:
                return self._send(404, {"error": "not_found"})
            if not service.settings.service_pubkey:
                return self._deny(403, "no_service_key", action)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._deny(400, "length", action)
            limit = servicelink.MAX_PACKAGE if action == "upload" else servicelink.MAX_BODY
            if length < 0 or length > limit:
                return self._deny(413, "too_large", action)
            try:
                data = servicelink.decode(self.headers.get(servicelink.HEADER_REQUEST) or "")
            except (ValueError, TypeError):
                return self._deny(400, "bad_request", action)
            sig = self.headers.get(servicelink.HEADER_SIGNATURE) or ""
            if data.get("action") != action or not isinstance(data.get("args"), dict):
                return self._deny(400, "bad_request", action)
            if not servicelink.verify(service.settings.service_pubkey, data, sig):
                return self._deny(403, "bad_signature", action)
            if not service.take_nonce(str(data.get("nonce") or "")):
                return self._deny(403, "nonce", action)
            # Inhalt lesen (Paket in eine Datei, sonst in den Speicher) und Prüfsumme gegen die Signatur vergleichen
            h = hashlib.sha256()
            tmp = None
            if action == "upload":
                fd, name = tempfile.mkstemp(dir=service.state_dir, prefix=".upload-")
                tmp = Path(name)
                with os.fdopen(fd, "wb") as f:
                    left = length
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            break
                        h.update(chunk)
                        f.write(chunk)
                        left -= len(chunk)
            else:
                h.update(self.rfile.read(length))
            body_sha = h.hexdigest()
            try:
                if body_sha != data.get("body_sha256"):
                    return self._deny(400, "body_checksum", action)
                if action == "status":
                    return self._send(200, service.status())
                if action == "progress":
                    return self._send(200, {"job": service.job_view(), "version": runner.installed_version()})
                if action == "upload":
                    code, out = service.accept_package(tmp, body_sha, data["args"].get("manifest"))
                    return self._send(code, out)
                code, out = service.start(action)
                return self._send(code, out)
            finally:
                if tmp is not None and tmp.exists():
                    tmp.unlink(missing_ok=True)

    return Handler


def make_server(service: Service, host: str, port: int = servicelink.PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(service))
    server.daemon_threads = True
    return server


def check_bind(host: str) -> None:
    """Nur eine link-local-Adresse (Servicekabel) – nie 0.0.0.0 oder eine Adresse im Praxisnetz."""
    if ipaddress.ip_address(host) not in LINK_LOCAL:
        raise SystemExit(f"Adresse {host} abgelehnt: das Servicekabel hört nur auf 169.254.x.x")


def main() -> None:
    ap = argparse.ArgumentParser(description="N-Fizyo Servicekabel (root)")
    ap.add_argument("--env", default="/etc/n-fizyo/n-fizyo.env")
    ap.add_argument("--bind", default=servicelink.BOX_ADDRESS)
    ap.add_argument("--port", type=int, default=servicelink.PORT)
    a = ap.parse_args()
    check_bind(a.bind)
    os.environ.setdefault("NFIZYO_ENV_FILE", a.env)
    service = Service(load_settings(a.env))
    server = make_server(service, a.bind, a.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        time.sleep(5)
        if service.restart_self and service.job_view().get("state") != "running":
            server.shutdown()
            import sys

            os.chdir(runner.APP_DIR / "app")
            os.execv(sys.executable, [sys.executable, "-m", "nfizyo.agent.service", "--env", a.env, "--bind", a.bind,
                                      "--port", str(a.port)])


if __name__ == "__main__":
    main()
