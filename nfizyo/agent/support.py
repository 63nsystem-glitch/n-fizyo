"""Supportzugang auf der Box – Teil des Runners (root). Nur nach Auftrag der Leitung (core/nsystem.py, HMAC-signiert).

Öffnen (support_open, 1–8 h):
  1) WireGuard-Tunnel «nfz-support» zum NSystem-Hub (nur ausgehend; die Box öffnet keinen Port im Internet).
     Über den Tunnel ist nur der Hub erreichbar (AllowedIPs = Hub/32) und auf der Box nur SSH (Port 22) vom Hub.
  2) SSH-Schlüssel von NSystem in ~nfizyo-support/.ssh/authorized_keys (Datei gehört root) – nur für diesen Benutzer, nur von
     der Hub-Adresse (from=…), ohne Weiterleitungen und ohne Terminal (restrict, kein pty), immer nur das Support-Werkzeug
     (command=…, deploy/n-fizyo-support-tool). Zusätzlich (deploy/support-setup.sh): sshd sperrt den Benutzer für jede andere
     Adresse und erzwingt das Werkzeug (Match User … ForceCommand), seine Anmelde-Shell IST das Werkzeug (keine bash),
     Passwort gesperrt, cron/at gesperrt.
  3) Ablauf merken (/var/lib/n-fizyo-runner/support.json) + systemd-Zeitgeber als zweite Sicherung.
Schliessen (früher durch die Leitung, durch NSystem, oder bei Ablauf – auch wenn die App nicht läuft):
  Schlüssel entfernen, offene Sitzungen des Support-Benutzers beenden, Tunnel abbauen, Zeitgeber stoppen.
Jeder Schritt und jede Verbindung (Tunnel, SSH-Anmeldung/-Abmeldung, Befehle des Werkzeugs) → Ereignis für das Protokoll
der Leitung (runner/events/*.json → box_events) und für die NSystem-Übersicht (nur Art und Zeit).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import safefs

IFACE = "nfz-support"
SUPPORT_USER = os.environ.get("NFIZYO_SUPPORT_USER", "nfizyo-support")
WG_DIR = Path(os.environ.get("NFIZYO_WG_DIR", "/etc/wireguard"))
SUPPORT_DIR = Path(os.environ.get("NFIZYO_SUPPORT_DIR", "/etc/n-fizyo/support"))          # wg.key (nur root)
SUPPORT_HOME = Path(os.environ.get("NFIZYO_SUPPORT_HOME", "/var/lib/n-fizyo-support"))    # .ssh/authorized_keys (gehört root)
TOOL = "/usr/local/sbin/n-fizyo-support-tool"
EXPIRE_UNIT = "n-fizyo-support-expire"
APP_DIR = Path(os.environ.get("NFIZYO_APP_DIR", "/opt/n-fizyo"))
APP_USER = os.environ.get("NFIZYO_APP_USER", "nfizyo")

HOSTPORT_RX = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*"
                         r"|\d{1,3}(?:\.\d{1,3}){3}):\d{2,5}")
WGKEY_RX = re.compile(r"[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=")
IPV4_RX = re.compile(r"(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)")
SSHKEY_RX = re.compile(r"(?:ssh-ed25519|sk-ssh-ed25519@openssh\.com|ecdsa-sha2-nistp256|sk-ecdsa-sha2-nistp256@openssh\.com|ssh-rsa)"
                       r" [A-Za-z0-9+/]{40,1200}={0,2}(?: [\w.@+-]{1,80})?")
CMD_RX = re.compile(r"[a-z0-9][a-z0-9 ._:-]{0,79}")


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, (p.stdout + ("\n" + p.stderr if p.stderr else "")).strip()[-4000:]
    except subprocess.TimeoutExpired:
        return 124, "Zeitlimit"
    except FileNotFoundError as exc:
        return 127, str(exc)


# --------------------------------------------------------------------------- Einstellungen (geprüft)
def config(settings) -> tuple[dict | None, str | None]:
    """Werte aus der .env prüfen (sie landen in einer Konfigurationsdatei, die wg-quick liest). → (cfg, Fehler)."""
    hub, pub, addr = settings.support_hub, settings.support_hub_pubkey, settings.support_address
    hub_ip = settings.support_hub_ip or "10.77.0.1"
    keys = [k.strip() for k in (settings.support_ssh_keys or "").split(";") if k.strip()]
    if not (hub or pub or addr or keys):
        return None, "not_configured"
    if not HOSTPORT_RX.fullmatch(hub or "") or not WGKEY_RX.fullmatch(pub or "") or not IPV4_RX.fullmatch(addr or "") \
            or not IPV4_RX.fullmatch(hub_ip) or addr == hub_ip:
        return None, "bad_config"
    if not keys or not all(SSHKEY_RX.fullmatch(k) for k in keys):
        return None, "bad_ssh_key"
    return {"hub": hub, "hub_pubkey": pub, "address": addr, "hub_ip": hub_ip, "ssh_keys": keys}, None


def key_file() -> Path:
    return SUPPORT_DIR / "wg.key"


def conf_file() -> Path:
    return WG_DIR / f"{IFACE}.conf"


def wg_conf(cfg: dict) -> str:
    return (
        "# N-Fizyo Supportzugang – vom Runner geschrieben, beim Schliessen gelöscht (docs/MERKEZ.md).\n"
        "# Über diesen Tunnel: nur der NSystem-Hub, und auf dieser Box nur SSH (Port 22) vom Hub.\n"
        "[Interface]\n"
        f"Address = {cfg['address']}/32\n"
        f"PostUp = wg set %i private-key {key_file()}\n"
        "PostUp = iptables -I INPUT -i %i -j DROP\n"
        f"PostUp = iptables -I INPUT -i %i -s {cfg['hub_ip']} -p tcp --dport 22 -j ACCEPT\n"
        f"PreDown = iptables -D INPUT -i %i -s {cfg['hub_ip']} -p tcp --dport 22 -j ACCEPT || true\n"
        "PreDown = iptables -D INPUT -i %i -j DROP || true\n"
        "\n[Peer]\n"
        f"PublicKey = {cfg['hub_pubkey']}\n"
        f"Endpoint = {cfg['hub']}\n"
        f"AllowedIPs = {cfg['hub_ip']}/32\n"
        "PersistentKeepalive = 25\n"
    )


def keys_file() -> Path:
    return SUPPORT_HOME / ".ssh" / "authorized_keys"


def authorized_keys(cfg: dict) -> str:
    # Kein «pty»: ohne Terminal gibt es keine Zeilen-Editoren/Pager, aus denen man eine Shell starten könnte.
    opts = f'from="{cfg["hub_ip"]}",restrict,command="{TOOL}"'
    return "".join(f"{opts} {k}\n" for k in cfg["ssh_keys"])


# --------------------------------------------------------------------------- Zustand und Ereignisse
def _state_file(state_dir: Path) -> Path:
    return state_dir / "support.json"


def load_state(state_dir: Path) -> dict:
    try:
        data = json.loads(_state_file(state_dir).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = state_dir / ".support.json.tmp"
    tmp.write_text(json.dumps(state))
    tmp.replace(_state_file(state_dir))


def emit(events_dir: Path, kind: str, detail: dict | None = None, at: float | None = None, chown=None) -> None:
    """Ereignis für die App (Protokoll der Leitung), Ordner <Daten>/runner/events. Der Ordner gehört dem App-Benutzer (sie
    löscht die Dateien nach dem Einlesen) – darum nur über safefs (keine Symlinks). chown: Datei dem App-Benutzer geben."""
    uid = f"run-{uuid.uuid4()}"
    text = json.dumps({"uid": uid, "kind": kind, "at": at or time.time(), "detail": detail or {}})
    safefs.write_json_atomic(events_dir.parent.parent, (events_dir.parent.name, events_dir.name),
                             f"{int((at or time.time()) * 1000)}-{uid}.json", text, owner=APP_USER if chown else None)


def status(settings, state_dir: Path) -> dict:
    cfg, _ = config(settings)
    st = load_state(state_dir)
    is_open = bool(st.get("open")) and float(st.get("until") or 0) > time.time()
    return {"configured": cfg is not None, "ready": cfg is not None and key_file().exists(), "open": is_open,
            "until": st.get("until") if is_open else None, "opened_at": st.get("opened_at") if is_open else None}


def _iface_up() -> bool:
    code, _ = _run(["wg", "show", IFACE], 10)
    return code == 0


def _write_private(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    tmp.replace(path)


def _schedule_expiry(seconds: int) -> None:
    """Zweite Sicherung: systemd schliesst den Zugang auch, wenn der Runner hängt. (Nach einem Neustart ist der Tunnel ohnehin aus.)"""
    _run(["systemctl", "stop", f"{EXPIRE_UNIT}.timer"], 15)
    _run(["systemd-run", f"--unit={EXPIRE_UNIT}", f"--on-active={max(1, seconds)}s", "--timer-property=AccuracySec=10s",
          f"--working-directory={APP_DIR / 'app'}", sys.executable, "-m", "nfizyo.agent.runner",
          "--env", os.environ.get("NFIZYO_ENV_FILE", "/etc/n-fizyo/n-fizyo.env"), "--support-expire"], 30)


def _tunnel_up(cfg: dict) -> tuple[bool, str]:
    if not key_file().exists():
        return False, "no_key"
    _write_private(conf_file(), wg_conf(cfg), 0o600)
    if _iface_up():
        _run(["wg-quick", "down", str(conf_file())], 60)
    code, text = _run(["wg-quick", "up", str(conf_file())], 60)
    return (code == 0), ("" if code == 0 else "tunnel")


def open_access(settings, state_dir: Path, events_dir: Path, request_id: str, hours: int, chown=None) -> bool:
    cfg, err = config(settings)
    if cfg is None:
        emit(events_dir, "support_open_failed", {"request_id": request_id, "error": err}, chown=chown)
        return False
    ok, err = _tunnel_up(cfg)
    if not ok:
        _teardown()
        emit(events_dir, "support_open_failed", {"request_id": request_id, "error": err}, chown=chown)
        return False
    _write_private(keys_file(), authorized_keys(cfg), 0o644)
    now = time.time()
    until = int(now + hours * 3600)
    save_state(state_dir, {"open": True, "request_id": request_id, "hours": hours, "opened_at": int(now), "until": until,
                           "tunnel_seen": False, "cursor": None})
    _schedule_expiry(until - int(now))
    emit(events_dir, "support_opened", {"request_id": request_id, "hours": hours, "until": until}, chown=chown)
    return True


def _remove_user_jobs() -> None:
    """Nichts darf den Zugang überdauern: cron/at sind für den Support-Benutzer gesperrt (support-setup.sh), trotzdem hier
    vorsorglich löschen – ebenso «linger» (systemd --user) und alle Sitzungen."""
    _run(["crontab", "-r", "-u", SUPPORT_USER], 15)
    code, text = _run(["atq"], 15)
    if code == 0:
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1] == SUPPORT_USER and parts[0].isdigit():
                _run(["atrm", parts[0]], 15)
    _run(["loginctl", "disable-linger", SUPPORT_USER], 15)
    _run(["loginctl", "terminate-user", SUPPORT_USER], 15)


def _teardown() -> None:
    if keys_file().parent.exists():
        _write_private(keys_file(), "", 0o644)
    _run(["pkill", "-KILL", "-u", SUPPORT_USER], 15)           # offene SSH-Sitzungen sofort beenden
    _remove_user_jobs()
    if conf_file().exists():
        _run(["wg-quick", "down", str(conf_file())], 60)
        conf_file().unlink(missing_ok=True)
    if _iface_up():
        _run(["ip", "link", "delete", "dev", IFACE], 15)
    _run(["systemctl", "stop", f"{EXPIRE_UNIT}.timer"], 15)


def close_access(state_dir: Path, events_dir: Path, reason: str, request_id: str | None = None, chown=None) -> bool:
    st = load_state(state_dir)
    was_open = bool(st.get("open"))
    _teardown()
    save_state(state_dir, {"open": False, "closed_at": int(time.time()), "reason": reason})
    if was_open or reason != "stale":
        emit(events_dir, "support_closed", {"reason": reason, "request_id": request_id or st.get("request_id")}, chown=chown)
    return was_open


# --------------------------------------------------------------------------- Laufende Prüfung (jede Runde des Runners)
def parse_journal(lines: list[str], user: str = SUPPORT_USER) -> tuple[list[tuple[str, dict, float]], str | None]:
    """journalctl -o json → [(Art, Detail, Zeit)], letzter Cursor. Nur Zeilen zum Support-Benutzer und zum Werkzeug."""
    out, cursor = [], None
    acc = re.compile(rf"Accepted publickey for {re.escape(user)} from (\S+)")
    bye = re.compile(rf"Disconnected from user {re.escape(user)} (\S+)")
    bad = re.compile(rf"(?:Failed publickey|Invalid user|Connection closed by authenticating user) (?:for )?{re.escape(user)}\b")
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        cursor = rec.get("__CURSOR") or cursor
        msg = str(rec.get("MESSAGE") or "")
        try:
            ts = int(rec.get("__REALTIME_TIMESTAMP") or 0) / 1e6 or time.time()
        except (TypeError, ValueError):
            ts = time.time()
        ident = rec.get("SYSLOG_IDENTIFIER")
        if ident == "n-fizyo-support":
            m = re.match(r"cmd=(.*)$", msg)
            if m and CMD_RX.fullmatch(m.group(1).strip()):
                out.append(("support_command", {"cmd": m.group(1).strip()}, ts))
        elif (m := acc.search(msg)):
            out.append(("support_ssh_login", {"from": m.group(1) if IPV4_RX.fullmatch(m.group(1)) else "?"}, ts))
        elif bye.search(msg):
            out.append(("support_ssh_logout", {}, ts))
        elif bad.search(msg):
            out.append(("support_ssh_denied", {}, ts))
    return out, cursor


def _journal(st: dict) -> list[str]:
    args = ["journalctl", "--no-pager", "-o", "json", "-t", "sshd", "-t", "sshd-session", "-t", "n-fizyo-support"]
    args += ["--after-cursor", st["cursor"]] if st.get("cursor") else ["--since", f"@{int(st.get('opened_at') or time.time())}"]
    code, text = _run(args, 30)
    return text.splitlines() if code == 0 else []


def _handshake() -> int:
    code, text = _run(["wg", "show", IFACE, "latest-handshakes"], 10)
    if code != 0:
        return 0
    try:
        return max((int(line.split()[-1]) for line in text.splitlines() if line.strip()), default=0)
    except ValueError:
        return 0


def tick(settings, state_dir: Path, events_dir: Path, chown=None, now: float | None = None) -> None:
    """Ablauf prüfen (auch nach Neustart), Tunnel nach Neustart wieder aufbauen, Verbindungen protokollieren."""
    now = now or time.time()
    st = load_state(state_dir)
    if not st.get("open"):
        if conf_file().exists():                     # Rest (z. B. Absturz beim Schliessen) → sicher zu
            close_access(state_dir, events_dir, "stale", chown=chown)
        return
    if now >= float(st.get("until") or 0):
        close_access(state_dir, events_dir, "expired", chown=chown)
        return
    if not _iface_up():                               # z. B. nach Neustart der Box innerhalb der erlaubten Zeit
        cfg, _ = config(settings)
        ok = cfg is not None and _tunnel_up(cfg)[0]
        if not ok:
            close_access(state_dir, events_dir, "failed", chown=chown)
            return
        _schedule_expiry(int(float(st["until"]) - now))
    if not st.get("tunnel_seen") and _handshake() > 0:
        st["tunnel_seen"] = True
        emit(events_dir, "support_tunnel_up", {}, chown=chown)
    found, cursor = parse_journal(_journal(st))
    for kind, detail, ts in found:
        emit(events_dir, kind, detail, at=ts, chown=chown)
    if cursor:
        st["cursor"] = cursor
    save_state(state_dir, st)
