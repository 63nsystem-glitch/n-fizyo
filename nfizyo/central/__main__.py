"""NSystem-Übersicht bedienen.

  python -m nfizyo.central --env /etc/n-fizyo-central/central.env init            # Signaturschlüssel erzeugen (einmalig)
  python -m nfizyo.central --env … add-staff --email s@nsystem.ch --name "S. Name"  # Passwort wird abgefragt
  python -m nfizyo.central --env … add-box --name "Physio Balance Biel"           # schreibt box-<id>.env (nur root lesbar)
  python -m nfizyo.central --env … list-boxes
  python -m nfizyo.central --env … support-peer --box bx_… --pubkey <WireGuard-Schlüssel der Box>   # Supportzugang einrichten
  python -m nfizyo.central --env … support-peers                                   # Peer-Datei für den Hub neu schreiben
  python -m nfizyo.central --env … releases                                        # Versionen aus dem signierten Index
  python -m nfizyo.central --env … run
Geheimnisse werden nie im Terminal ausgegeben: das Gerätegeheimnis steht nur in der erzeugten Datei, die auf die Box kommt.
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
from pathlib import Path

from aiohttp import web

from ..agent import signing
from ..core.db import Database
from . import ops, releases
from .app import add_box, add_staff, create_central_app
from .config import load

HERE = Path(__file__).parent


def _db(settings) -> Database:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    db.migrate([("central", HERE / "migrations")])
    return db


def public_key(settings) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_private_key
    import base64

    key = load_pem_private_key(settings.signing_key_path.read_bytes(), password=None)
    return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def cmd_init(settings) -> None:
    _db(settings)
    if settings.signing_key_path.exists():
        print("Signaturschlüssel existiert bereits (wird nicht ersetzt).")
    else:
        pem, _ = signing.new_signing_key()
        fd = os.open(settings.signing_key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        print(f"Signaturschlüssel erzeugt: {settings.signing_key_path} (nur dieser Dienst darf ihn lesen).")
    print("Öffentlicher Schlüssel für die Boxen (nicht geheim):")
    print(public_key(settings))


def cmd_add_staff(settings, email: str, name: str) -> None:
    db = _db(settings)
    pw = getpass.getpass("Passwort (mind. 12 Zeichen, wird nicht angezeigt): ")
    if pw != getpass.getpass("Wiederholen: "):
        raise SystemExit("Passwörter verschieden.")
    add_staff(db, email, name, pw)
    print(f"Konto {email} angelegt. Beim ersten Anmelden wird die Zwei-Schritt-Anmeldung eingerichtet.")


def cmd_add_box(settings, name: str, out_dir: str | None) -> None:
    db = _db(settings)
    box_id, key = add_box(db, settings.secret_key, name)
    target = Path(out_dir or settings.data_dir) / f"box-{box_id}.env"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"# N-Fizyo Box «{name}» – auf der Box mit deploy/connect-central.sh einspielen, danach diese Datei löschen.\n")
        f.write(f"NFIZYO_CENTRAL_URL={settings.base_url}\n")
        f.write(f"NFIZYO_CENTRAL_BOX_ID={box_id}\n")
        f.write(f"NFIZYO_CENTRAL_BOX_KEY={key}\n")
        f.write(f"NFIZYO_CENTRAL_COMMAND_PUBKEY={public_key(settings)}\n")
        if settings.release_base and settings.release_pubkey:
            f.write(f"NFIZYO_RELEASE_BASE={settings.release_base}\n")
            f.write(f"NFIZYO_RELEASE_PUBKEY={settings.release_pubkey}\n")
    print(f"Box angelegt: {box_id} ({name})")
    print(f"Verbindungsdatei (enthält das Gerätegeheimnis, nur root lesbar): {target}")
    print("Auf die Box kopieren (z. B. scp) und dort: sudo bash deploy/connect-central.sh <Datei>")


def _write_peers(settings, db) -> Path:
    target = settings.wg_peers_path
    tmp = target.with_name(".wg-peers.conf.tmp")
    tmp.write_text(ops.peers_conf(db))
    os.chmod(tmp, 0o644)   # nur öffentliche Schlüssel und Adressen; der Hub-Dienst (root) liest sie
    tmp.replace(target)
    return target


def cmd_support_peer(settings, box_id: str, pubkey: str, out_dir: str | None) -> None:
    """Box am Support-Hub eintragen und die Einstellungen für die Box schreiben (keine Geheimnisse: nur öffentliche Schlüssel)."""
    missing = [k for k, v in (("NFIZYO_CENTRAL_SUPPORT_ENDPOINT", settings.support_endpoint),
                              ("NFIZYO_CENTRAL_SUPPORT_HUB_PUBKEY", settings.support_hub_pubkey),
                              ("NFIZYO_CENTRAL_SUPPORT_SSH_KEYS", settings.support_ssh_keys)) if not v]
    if missing:
        raise SystemExit("Zuerst in central.env eintragen: " + ", ".join(missing) + " (docs/MERKEZ.md › Supportzugang).")
    db = _db(settings)
    try:
        ip = ops.assign_peer(db, settings, box_id, pubkey.strip())
    except ValueError as exc:
        raise SystemExit(str(exc))
    peers = _write_peers(settings, db)
    target = Path(out_dir or settings.data_dir) / f"support-{box_id}.env"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"# N-Fizyo Supportzugang für {box_id} – auf der Box mit deploy/connect-central.sh einspielen.\n")
        f.write(f"NFIZYO_SUPPORT_HUB={settings.support_endpoint}\n")
        f.write(f"NFIZYO_SUPPORT_HUB_PUBKEY={settings.support_hub_pubkey}\n")
        f.write(f"NFIZYO_SUPPORT_ADDRESS={ip}\n")
        f.write(f"NFIZYO_SUPPORT_HUB_IP={ops.hub_ip(settings)}\n")
        f.write(f"NFIZYO_SUPPORT_SSH_KEYS={settings.support_ssh_keys}\n")
    print(f"Box {box_id}: Tunnel-Adresse {ip}. Peers für den Hub: {peers} (der Hub übernimmt sie selbst).")
    print(f"Einstellungen für die Box: {target} → auf die Box kopieren, dort: sudo bash deploy/connect-central.sh <Datei>")


def cmd_releases(settings) -> None:
    items, err = releases.available(settings, fresh=True)
    if err:
        raise SystemExit(err)
    for m in items:
        print(f"{m['version']:10} {m['built_at'][:10]}  {m['sha256'][:16]}…  {m['file']}")
    if not items:
        print("Keine Versionen (Release-Ablage leer oder nicht eingerichtet).")


def cmd_list(settings) -> None:
    db = _db(settings)
    for b in db.query("SELECT id, name, last_report_at, last_version, archived FROM boxes ORDER BY name"):
        print(f"{b['id']}  {b['name']:30}  {b['last_version'] or '–':10}  {b['last_report_at'] or 'nie'}{'  (archiviert)' if b['archived'] else ''}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="nfizyo.central", description="NSystem-Übersicht")
    p.add_argument("--env", default="/etc/n-fizyo-central/central.env")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    s = sub.add_parser("add-staff"); s.add_argument("--email", required=True); s.add_argument("--name", required=True)
    b = sub.add_parser("add-box"); b.add_argument("--name", required=True); b.add_argument("--out", default=None)
    sub.add_parser("list-boxes")
    sp = sub.add_parser("support-peer"); sp.add_argument("--box", required=True); sp.add_argument("--pubkey", required=True)
    sp.add_argument("--out", default=None)
    sub.add_parser("support-peers")
    sub.add_parser("releases")
    sub.add_parser("run")
    a = p.parse_args()
    settings = load(a.env)
    if a.cmd == "init":
        cmd_init(settings)
    elif a.cmd == "add-staff":
        cmd_add_staff(settings, a.email, a.name)
    elif a.cmd == "add-box":
        cmd_add_box(settings, a.name, a.out)
    elif a.cmd == "list-boxes":
        cmd_list(settings)
    elif a.cmd == "support-peer":
        cmd_support_peer(settings, a.box, a.pubkey, a.out)
    elif a.cmd == "support-peers":
        print(_write_peers(settings, _db(settings)))
    elif a.cmd == "releases":
        cmd_releases(settings)
    elif a.cmd == "run":
        if not settings.signing_key_path.exists():
            raise SystemExit("Zuerst «init» ausführen (Signaturschlüssel).")
        web.run_app(create_central_app(settings), host=settings.host, port=settings.port, access_log=None)


if __name__ == "__main__":
    main()
