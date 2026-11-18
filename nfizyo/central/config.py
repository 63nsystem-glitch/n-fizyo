"""Einstellungen der NSystem-Übersicht (Datei /etc/n-fizyo-central/central.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..core.config import _proxies, _read_env_file


@dataclass(frozen=True)
class CentralSettings:
    data_dir: Path
    secret_key: bytes = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8095
    base_url: str = "http://127.0.0.1:8095"
    ntfy_url: str = ""                          # z. B. https://ntfy.sh/<geheimes-thema> oder eigener ntfy-Server
    ntfy_token: str = field(default="", repr=False)
    silence_minutes: int = 30                   # so lange ohne Meldung → Warnung
    command_minutes: int = 60                   # so lange ist ein Befehl gültig
    trusted_proxies: tuple[str, ...] = ("127.0.0.1", "::1")   # Caddy davor: nur von hier gilt X-Forwarded-For
    # Releases (tools/release.py): Ablage mit index.json – Ordner auf diesem Server ODER https-Adresse; öffentlicher Schlüssel
    release_dir: str = ""                       # z. B. /srv/n-fizyo-releases (gleicher Server, von Caddy ausgeliefert)
    release_base: str = ""                      # z. B. https://releases.nsystem.ch (wenn die Ablage woanders liegt)
    release_pubkey: str = ""                    # öffentlicher Release-Schlüssel (Base64) – nicht geheim
    # Supportzugang: WireGuard-Hub auf diesem Server (deploy/install-support-hub.sh)
    support_endpoint: str = ""                  # wie die Boxen den Hub erreichen, z. B. hub.nsystem.ch:51820
    support_hub_pubkey: str = ""                # öffentlicher WireGuard-Schlüssel des Hubs
    support_net: str = "10.77.0.0/24"           # Tunnel-Netz; .1 = Hub, Boxen ab .10
    support_ssh_keys: str = ""                  # öffentliche SSH-Schlüssel von NSystem (mit «;» getrennt)
    support_ssh_host: str = ""                  # SSH-Zugang zum Hub (für den Hinweis «ssh -J …»), z. B. admin@hub.nsystem.ch

    @property
    def backup_status_path(self) -> Path:
        """Ergebnis der nächtlichen Sicherung des Panels (n-fizyo-backup schreibt es, der Dienst liest es)."""
        return self.data_dir / "backup-status.json"

    @property
    def wg_peers_path(self) -> Path:
        """Peers für den WireGuard-Hub (nur öffentliche Schlüssel + Adressen) – nfz-hub-sync liest die Datei."""
        return self.data_dir / "wg-peers.conf"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "central.sqlite3"

    @property
    def signing_key_path(self) -> Path:
        return self.data_dir / "command-signing.pem"

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")

    def with_overrides(self, **kw) -> "CentralSettings":
        return replace(self, **kw)


def load(env_file: str | None = None, **overrides) -> CentralSettings:
    values = _read_env_file(Path(env_file)) if env_file else {}

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key, values.get(key, default))

    secret = get("NFIZYO_CENTRAL_SECRET_KEY")
    if not secret and "secret_key" not in overrides:
        raise RuntimeError("NFIZYO_CENTRAL_SECRET_KEY fehlt (install-central.sh erzeugt ihn).")
    s = CentralSettings(
        data_dir=Path(get("NFIZYO_CENTRAL_DATA_DIR", "/var/lib/n-fizyo-central")),
        secret_key=secret.encode() if secret else b"",
        host=get("NFIZYO_CENTRAL_HOST", "127.0.0.1"),
        port=int(get("NFIZYO_CENTRAL_PORT", "8095") or 8095),
        base_url=get("NFIZYO_CENTRAL_BASE_URL", "http://127.0.0.1:8095").rstrip("/"),
        ntfy_url=get("NFIZYO_CENTRAL_NTFY_URL").rstrip("/"),
        ntfy_token=get("NFIZYO_CENTRAL_NTFY_TOKEN"),
        silence_minutes=int(get("NFIZYO_CENTRAL_SILENCE_MINUTES", "30") or 30),
        trusted_proxies=_proxies(get("NFIZYO_CENTRAL_TRUSTED_PROXIES")),
        release_dir=get("NFIZYO_CENTRAL_RELEASE_DIR").strip(),
        release_base=get("NFIZYO_CENTRAL_RELEASE_BASE").strip().rstrip("/"),
        release_pubkey=get("NFIZYO_CENTRAL_RELEASE_PUBKEY").strip(),
        support_endpoint=get("NFIZYO_CENTRAL_SUPPORT_ENDPOINT").strip(),
        support_hub_pubkey=get("NFIZYO_CENTRAL_SUPPORT_HUB_PUBKEY").strip(),
        support_net=get("NFIZYO_CENTRAL_SUPPORT_NET", "10.77.0.0/24").strip() or "10.77.0.0/24",
        support_ssh_keys=get("NFIZYO_CENTRAL_SUPPORT_SSH_KEYS").strip(),
        support_ssh_host=get("NFIZYO_CENTRAL_SUPPORT_SSH_HOST").strip(),
    )
    return s.with_overrides(**overrides) if overrides else s
