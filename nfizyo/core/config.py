"""Einstellungen aus Umgebungsvariablen bzw. einer .env-Datei."""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path

log = logging.getLogger(__name__)


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _flag(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "ja", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    secret_key: bytes = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8080
    base_url: str = "http://127.0.0.1:8080"
    demo: bool = False
    default_lang: str = "de"
    stt_engine: str = "none"
    stt_model: str = "large-v3-turbo"
    stt_device: str = "cuda"
    llm_engine: str = "rules"
    llm_model: str = "qwen2.5:7b-instruct"
    ollama_url: str = "http://127.0.0.1:11434"
    translate_engine: str = "none"
    translate_model: str = "translategemma:12b"
    allow_remote_ai: bool = False
    # KI-Betrieb: local = auf dieser Box (GPU) | remote = KI-Server von NSystem in der Schweiz (Daten bleiben hier,
    # nur Audio/Text der jeweiligen Aufgabe geht verschlüsselt hin; der Server speichert nichts)
    ai_mode: str = "local"
    ai_server_url: str = ""
    ai_token: str = field(default="", repr=False)  # nie in Logs
    # NUR für Vorführungen (NFIZYO_AI_MODE=cloud_demo, nur mit NFIZYO_DEMO=1 und fiktiven Daten): OpenAI-kompatibler
    # Cloud-Dienst für Diktat, Notizentwurf, Berichte, Patienteninformation und Übersetzung. Nie in echten Praxen.
    cloud_ai_url: str = ""                                  # z. B. https://api.openai.com/v1 oder https://api.mistral.ai/v1
    cloud_ai_key: str = field(default="", repr=False)       # API-Schlüssel – nur in der .env, nie in Logs, Oberfläche oder Doku
    cloud_ai_stt_model: str = ""
    cloud_ai_llm_model: str = ""
    cloud_ai_translate_model: str = ""                      # leer = wie cloud_ai_llm_model
    cloud_ai_timeout: float = 60.0                          # Sekunden pro Anfrage
    cloud_ai_reasoning: str = ""                            # optional: reasoning_effort (z. B. none), leer = nicht senden
    delete_audio_after_approval: bool = True
    # SMS: log = nichts verlässt die Box (Demo, Tests) | live = Anbieter laut Praxis-Einstellung (ASPSMS oder eCall)
    sms_engine: str = "log"
    aspsms_userkey: str = field(default="", repr=False)
    aspsms_password: str = field(default="", repr=False)
    ecall_username: str = field(default="", repr=False)
    ecall_password: str = field(default="", repr=False)
    # E-Mail an Patient/innen (Terminbestätigung, Erinnerung, Rechnung als PDF): log = nichts verlässt die Box | live = SMTP
    # Nur mit Einverständnis der Patientin / des Patienten (E-Mail ist nicht verschlüsselt). Berichte an Ärzt/innen: HIN.
    mail_engine: str = "log"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_tls: str = "starttls"        # starttls | ssl | none
    smtp_user: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_from: str = ""               # Absenderadresse, z. B. termine@praxis.ch (Antworten gehen an die E-Mail der Praxis)
    secure_cookies: bool = False
    # Zwei-Schritt-Anmeldung (Authenticator-App) für alle Praxiskonten
    require_2fa: bool = True
    # Vorgeschaltete Proxys (Caddy auf derselben Box): nur von diesen Adressen wird X-Forwarded-For geglaubt.
    # IP-Adressen oder Netze (CIDR), z. B. "127.0.0.1,::1" (Standard). "none" = keinem Proxy glauben.
    trusted_proxies: tuple[str, ...] = ("127.0.0.1", "::1")
    # Praxiskonten: nach so vielen Stunden ohne Klick abgemeldet (zusätzlich zur festen Höchstdauer von 14 Tagen)
    staff_idle_hours: float = 8.0
    # HIN: gesicherte E-Mail über den HIN Client (127.0.0.1) oder ein HIN-Gateway der Praxis
    hin_practice: str = ""            # Kurzname (slug) der Praxis, zu der das HIN-Postfach gehört
    hin_from: str = ""                # HIN-Adresse der Praxis, z. B. praxis@hin.ch
    hin_smtp_host: str = "127.0.0.1"
    hin_smtp_port: int = 5018
    hin_imap_host: str = "127.0.0.1"
    hin_imap_port: int = 5020
    hin_tls: str = "none"             # none (HIN Client verschlüsselt selbst) | starttls | ssl
    hin_user: str = ""
    hin_password: str = field(default="", repr=False)
    hin_poll_minutes: int = 5
    # MediData (elektronische Rechnung): off = XML nur erzeugen | test | production
    medidata_mode: str = "off"
    medidata_url: str = ""            # Webservice https://services[-acc].medidata.ch/md/srv/id0 oder Box https://box:8100
    medidata_client_id: str = ""
    medidata_user: str = ""
    medidata_password: str = field(default="", repr=False)
    # Praxis (Kurzname), der der MediData-Zugang gehört: Antworten, Benachrichtigungen und Verzeichnis landen dort.
    # Leer = die einzige Praxis der Box (bei mehreren Praxen Pflicht).
    medidata_practice: str = ""
    # MediData-Webservice: Client-Zertifikat (gegenseitiges TLS). Pfade zu Dateien, die nur der Dienst lesen darf (chmod 600).
    # Leer = ohne Client-Zertifikat (MediData-Box / VA). Anleitung: docs/VERBINDUNGSTEST.md › MediData.
    medidata_cert: str = ""           # Zertifikatskette (PEM), CN = X-Client-ID
    medidata_key: str = ""            # privater Schlüssel (PEM, ohne Passwort)
    medidata_ca: str = ""             # zusätzliche CA-Datei (PEM), z. B. selbstsigniertes Zertifikat der Testumgebung
    medidata_service_url: str = "https://services.medidata.ch"   # Webservice (Zertifikat erneuern)
    medidata_appliance_id: str = ""   # <applianceid> im Pfad md/srv/<applianceid>/client/
    # NSystem-Übersicht: die Box meldet ihren Zustand (keine Patientendaten) und holt signierte Befehle ab
    central_url: str = ""
    central_box_id: str = ""
    central_box_key: str = field(default="", repr=False)   # Gerätegeheimnis (HMAC) für Meldungen an NSystem
    central_command_pubkey: str = ""                        # öffentlicher Schlüssel (Ed25519) für NSystem-Befehle
    # Neue Versionen (Befehl «Neue Version»): nur von dieser Adresse und nur mit Signatur des NSystem-Release-Schlüssels
    release_base: str = ""                                  # z. B. https://releases.nsystem.ch
    release_pubkey: str = ""                                # öffentlicher Release-Schlüssel (Ed25519, Base64) – nicht geheim
    # Servicekabel (agent/service.py): öffentlicher Service-Schlüssel des NSystem-Laptops (Ed25519, Base64) – nicht geheim
    service_pubkey: str = ""
    # Supportzugang (nur wenn die Leitung ihn öffnet): WireGuard-Tunnel zum NSystem-Hub, SSH nur über den Tunnel (docs/MERKEZ.md)
    support_hub: str = ""                                   # Hub-Adresse host:port, z. B. hub.nsystem.ch:51820
    support_hub_pubkey: str = ""                            # öffentlicher WireGuard-Schlüssel des Hubs
    support_address: str = ""                               # Tunnel-Adresse dieser Box, z. B. 10.77.0.12
    support_hub_ip: str = "10.77.0.1"                       # Tunnel-Adresse des Hubs (nur von hier ist SSH erlaubt)
    support_ssh_keys: str = ""                              # öffentliche SSH-Schlüssel von NSystem (mehrere mit «;» getrennt)
    # Video-Sitzungen: eigener Jitsi-Server in der Schweiz (deploy/jitsi). Ohne URL ist Video ausgeschaltet.
    video_url: str = ""                                     # z. B. https://video.physio.example.ch
    video_app_id: str = "nfizyo"                            # JWT_APP_ID auf dem Jitsi-Server
    video_secret: str = field(default="", repr=False)       # JWT_APP_SECRET – nur in der .env, nie im Chat oder Code
    # bexio (Buchhaltung): entweder Personal Access Token (eine Praxis pro Box) oder OAuth-App von NSystem (Client-ID/Secret)
    bexio_token: str = field(default="", repr=False)
    # Praxis (Kurzname oder Nummer), der das Token gehört – nur sie sieht bexio im Token-Modus. Leer = die einzige Praxis der Box
    # (bei mehreren Praxen Pflicht, sonst ist das Token für niemanden aktiv).
    bexio_practice: str = ""
    bexio_client_id: str = ""
    bexio_client_secret: str = field(default="", repr=False)
    bexio_api: str = "https://api.bexio.com"
    bexio_auth: str = "https://auth.bexio.com/realms/bexio/protocol/openid-connect"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "n-fizyo.sqlite3"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def scan_dir(self) -> Path:
        """Scanner legt PDFs hier ab: scan-eingang/<praxis-kurzname>/datei.pdf"""
        return self.data_dir / "scan-eingang"

    def with_overrides(self, **kwargs) -> "Settings":
        return replace(self, **kwargs)


def _proxies(value: str | None) -> tuple[str, ...]:
    """NFIZYO_TRUSTED_PROXIES: leer = Standard (nur diese Box), «none» = keinem Proxy glauben."""
    v = (value or "").strip()
    if not v:
        return ("127.0.0.1", "::1")
    if v.lower() in {"none", "off", "0"}:
        return ()
    return tuple(p.strip() for p in v.split(",") if p.strip())


def _hours(value: str | None, default: float) -> float:
    try:
        h = float(value) if value not in (None, "") else default
    except ValueError:
        return default
    return h if h > 0 else default


def _sms_mode(value: str | None) -> str:
    """log (Standard) oder live. Ältere Werte aspsms/ecall bedeuten live."""
    v = (value or "log").strip().lower()
    return "live" if v in {"live", "aspsms", "ecall"} else "log"


def load_settings(env_file: str | Path | None = None, **overrides) -> Settings:
    """Liest Einstellungen. Reihenfolge: overrides > Umgebungsvariablen > .env-Datei."""
    file_values = _read_env_file(Path(env_file)) if env_file else _read_env_file(Path(".env"))

    def get(key: str, default: str | None = None) -> str | None:
        return os.environ.get(key, file_values.get(key, default))

    demo = bool(overrides.get("demo", _flag(get("NFIZYO_DEMO"), False)))
    secret = get("NFIZYO_SECRET_KEY") or ""
    if not secret and "secret_key" in overrides:
        secret = "aus-overrides"
    if not secret:
        if not demo:
            raise RuntimeError(
                "NFIZYO_SECRET_KEY fehlt. deploy/install.sh erzeugt ihn automatisch in der .env-Datei."
            )
        log.warning("Kein NFIZYO_SECRET_KEY gesetzt – Demo-Modus verwendet einen temporären Schlüssel.")
        secret = secrets.token_hex(32)

    base_url = get("NFIZYO_BASE_URL", "http://127.0.0.1:8080") or "http://127.0.0.1:8080"
    settings = Settings(
        data_dir=Path(get("NFIZYO_DATA_DIR", "./data/runtime") or "./data/runtime").resolve(),
        secret_key=secret.encode("utf-8"),
        host=get("NFIZYO_HOST", "127.0.0.1") or "127.0.0.1",
        port=int(get("NFIZYO_PORT", "8080") or 8080),
        base_url=base_url.rstrip("/"),
        demo=demo,
        default_lang=(get("NFIZYO_DEFAULT_LANG", "de") or "de")[:2],
        stt_engine=get("NFIZYO_STT", "none") or "none",
        stt_model=get("NFIZYO_STT_MODEL", "large-v3-turbo") or "large-v3-turbo",
        stt_device=get("NFIZYO_STT_DEVICE", "cuda") or "cuda",
        llm_engine=get("NFIZYO_LLM", "rules") or "rules",
        llm_model=get("NFIZYO_LLM_MODEL", "qwen2.5:7b-instruct") or "qwen2.5:7b-instruct",
        ollama_url=(get("NFIZYO_OLLAMA_URL", "http://127.0.0.1:11434") or "").rstrip("/"),
        translate_engine=get("NFIZYO_TRANSLATE", "none") or "none",
        translate_model=get("NFIZYO_TRANSLATE_MODEL", "translategemma:12b") or "translategemma:12b",
        allow_remote_ai=_flag(get("NFIZYO_ALLOW_REMOTE_AI"), False),
        ai_mode=(get("NFIZYO_AI_MODE", "local") or "local").strip().lower(),
        ai_server_url=(get("NFIZYO_AI_SERVER_URL", "") or "").rstrip("/"),
        ai_token=get("NFIZYO_AI_TOKEN", "") or "",
        cloud_ai_url=(get("NFIZYO_CLOUD_AI_URL", "") or "").strip().rstrip("/"),
        cloud_ai_key=(get("NFIZYO_CLOUD_AI_KEY", "") or "").strip(),
        cloud_ai_stt_model=(get("NFIZYO_CLOUD_AI_STT_MODEL", "") or "").strip(),
        cloud_ai_llm_model=(get("NFIZYO_CLOUD_AI_LLM_MODEL", "") or "").strip(),
        cloud_ai_translate_model=(get("NFIZYO_CLOUD_AI_TRANSLATE_MODEL", "") or get("NFIZYO_CLOUD_AI_LLM_MODEL", "") or "").strip(),
        cloud_ai_timeout=_hours(get("NFIZYO_CLOUD_AI_TIMEOUT"), 60.0),
        cloud_ai_reasoning=(get("NFIZYO_CLOUD_AI_REASONING", "") or "").strip().lower(),
        delete_audio_after_approval=_flag(get("NFIZYO_DELETE_AUDIO_AFTER_APPROVAL"), True),
        sms_engine=_sms_mode(get("NFIZYO_SMS", "log")),
        aspsms_userkey=get("NFIZYO_ASPSMS_USERKEY", "") or "",
        aspsms_password=get("NFIZYO_ASPSMS_PASSWORD", "") or "",
        ecall_username=get("NFIZYO_ECALL_USERNAME", "") or "",
        ecall_password=get("NFIZYO_ECALL_PASSWORD", "") or "",
        mail_engine="live" if (get("NFIZYO_MAIL", "log") or "log").strip().lower() == "live" else "log",
        smtp_host=get("NFIZYO_SMTP_HOST", "") or "",
        smtp_port=int(get("NFIZYO_SMTP_PORT", "587") or 587),
        smtp_tls=(get("NFIZYO_SMTP_TLS", "starttls") or "starttls").lower(),
        smtp_user=get("NFIZYO_SMTP_USER", "") or "",
        smtp_password=get("NFIZYO_SMTP_PASSWORD", "") or "",
        smtp_from=get("NFIZYO_SMTP_FROM", "") or "",
        secure_cookies=base_url.startswith("https://"),
        require_2fa=_flag(get("NFIZYO_REQUIRE_2FA"), not demo),
        trusted_proxies=_proxies(get("NFIZYO_TRUSTED_PROXIES")),
        staff_idle_hours=_hours(get("NFIZYO_STAFF_IDLE_HOURS"), 8.0),
        hin_practice=get("NFIZYO_HIN_PRACTICE", "") or "",
        hin_from=get("NFIZYO_HIN_FROM", "") or "",
        hin_smtp_host=get("NFIZYO_HIN_SMTP_HOST", "127.0.0.1") or "127.0.0.1",
        hin_smtp_port=int(get("NFIZYO_HIN_SMTP_PORT", "5018") or 5018),
        hin_imap_host=get("NFIZYO_HIN_IMAP_HOST", "127.0.0.1") or "127.0.0.1",
        hin_imap_port=int(get("NFIZYO_HIN_IMAP_PORT", "5020") or 5020),
        hin_tls=(get("NFIZYO_HIN_TLS", "none") or "none").lower(),
        hin_user=get("NFIZYO_HIN_USER", "") or "",
        hin_password=get("NFIZYO_HIN_PASSWORD", "") or "",
        hin_poll_minutes=max(1, int(get("NFIZYO_HIN_POLL_MINUTES", "5") or 5)),
        medidata_mode=(get("NFIZYO_MEDIDATA", "off") or "off").lower(),
        medidata_url=(get("NFIZYO_MEDIDATA_URL", "") or "").rstrip("/"),
        medidata_client_id=get("NFIZYO_MEDIDATA_CLIENT_ID", "") or "",
        medidata_user=get("NFIZYO_MEDIDATA_USER", "") or "",
        medidata_password=get("NFIZYO_MEDIDATA_PASSWORD", "") or "",
        medidata_practice=(get("NFIZYO_MEDIDATA_PRACTICE", "") or "").strip(),
        medidata_cert=(get("NFIZYO_MEDIDATA_CERT", "") or "").strip(),
        medidata_key=(get("NFIZYO_MEDIDATA_KEY", "") or "").strip(),
        medidata_ca=(get("NFIZYO_MEDIDATA_CA", "") or "").strip(),
        medidata_service_url=(get("NFIZYO_MEDIDATA_SERVICE_URL", "https://services.medidata.ch")
                              or "https://services.medidata.ch").strip().rstrip("/"),
        medidata_appliance_id=(get("NFIZYO_MEDIDATA_APPLIANCE_ID", "") or "").strip(),
        central_url=(get("NFIZYO_CENTRAL_URL", "") or "").rstrip("/"),
        central_box_id=get("NFIZYO_CENTRAL_BOX_ID", "") or "",
        central_box_key=get("NFIZYO_CENTRAL_BOX_KEY", "") or "",
        central_command_pubkey=get("NFIZYO_CENTRAL_COMMAND_PUBKEY", "") or "",
        release_base=(get("NFIZYO_RELEASE_BASE", "") or "").strip().rstrip("/"),
        release_pubkey=(get("NFIZYO_RELEASE_PUBKEY", "") or "").strip(),
        service_pubkey=(get("NFIZYO_SERVICE_PUBKEY", "") or "").strip(),
        support_hub=(get("NFIZYO_SUPPORT_HUB", "") or "").strip(),
        support_hub_pubkey=(get("NFIZYO_SUPPORT_HUB_PUBKEY", "") or "").strip(),
        support_address=(get("NFIZYO_SUPPORT_ADDRESS", "") or "").strip(),
        support_hub_ip=(get("NFIZYO_SUPPORT_HUB_IP", "10.77.0.1") or "10.77.0.1").strip(),
        support_ssh_keys=(get("NFIZYO_SUPPORT_SSH_KEYS", "") or "").strip(),
        video_url=(get("NFIZYO_VIDEO_URL", "") or "").rstrip("/"),
        video_app_id=get("NFIZYO_VIDEO_APP_ID", "nfizyo") or "nfizyo",
        video_secret=get("NFIZYO_VIDEO_SECRET", "") or "",
        bexio_token=get("NFIZYO_BEXIO_TOKEN", "") or "",
        bexio_practice=(get("NFIZYO_BEXIO_PRACTICE", "") or "").strip(),
        bexio_client_id=get("NFIZYO_BEXIO_CLIENT_ID", "") or "",
        bexio_client_secret=get("NFIZYO_BEXIO_CLIENT_SECRET", "") or "",
        bexio_api=(get("NFIZYO_BEXIO_API", "https://api.bexio.com") or "https://api.bexio.com").rstrip("/"),
        bexio_auth=(get("NFIZYO_BEXIO_AUTH", "https://auth.bexio.com/realms/bexio/protocol/openid-connect")
                    or "https://auth.bexio.com/realms/bexio/protocol/openid-connect").rstrip("/"),
    )
    if overrides:
        settings = settings.with_overrides(**overrides)
    return settings
