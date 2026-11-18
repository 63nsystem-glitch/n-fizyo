#!/usr/bin/env python3
"""N-Fizyo-Release bauen, signieren und veröffentlichen (Rechner von NSystem, nicht die Praxis-Box).

  export NFIZYO_RELEASE_KEY=/pfad/ausserhalb/des/projekts/nsystem-release.pem
  python3 tools/release.py keygen                      # einmalig: Ed25519-Schlüssel (0600, wird nie überschrieben) + öffentlicher Teil
  python3 tools/release.py pubkey                      # öffentlicher Schlüssel (für NFIZYO_RELEASE_PUBKEY / _CENTRAL_RELEASE_PUBKEY)
  python3 tools/release.py build [--out dist]          # Archiv + .sha256 + signiertes Manifest
  python3 tools/release.py publish --dir /srv/n-fizyo-releases [--rsync nsystem@panel.nsystem.ch:/srv/n-fizyo-releases]
  python3 tools/release.py verify dist/n-fizyo-0.9.0.manifest.json --pubkey <Base64>

Der private Schlüssel liegt nie im Projektordner (wird abgelehnt), nie im Git, nie im Chat. Ablauf: docs/MERKEZ.md › «Sürümler».
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nfizyo.agent import release, signing  # noqa: E402


def _key_path(must_exist: bool = True) -> Path:
    raw = os.environ.get("NFIZYO_RELEASE_KEY", "").strip()
    if not raw:
        raise SystemExit("NFIZYO_RELEASE_KEY fehlt (Pfad zur Schlüsseldatei ausserhalb des Projekts).")
    path = Path(raw).expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise SystemExit("Der Release-Schlüssel darf nicht im Projektordner liegen (Git!).")
    if must_exist:
        if not path.exists():
            raise SystemExit(f"Schlüsseldatei fehlt: {path} (zuerst «keygen»).")
        if path.stat().st_mode & 0o077:
            raise SystemExit(f"Schlüsseldatei darf nur für dich lesbar sein: chmod 600 {path}")
    return path


def cmd_keygen(_a) -> None:
    path = _key_path(must_exist=False)
    if path.exists():
        raise SystemExit(f"Schlüssel gibt es schon ({path}) – wird nie überschrieben.")
    path.parent.mkdir(parents=True, exist_ok=True)
    pem, pub = signing.new_signing_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    print(f"Release-Schlüssel erzeugt: {path} (nur du; Sicherungskopie offline aufbewahren).")
    print("Öffentlicher Schlüssel (nicht geheim) für NFIZYO_RELEASE_PUBKEY und NFIZYO_CENTRAL_RELEASE_PUBKEY:")
    print(pub)


def cmd_pubkey(_a) -> None:
    print(signing.public_of(_key_path().read_bytes()))


def cmd_build(a) -> None:
    pem = _key_path().read_bytes()
    out = Path(a.out).resolve()
    version = release.repo_version(ROOT)
    archive, sha = release.build_archive(ROOT, out, version, mtime=int(os.environ.get("SOURCE_DATE_EPOCH") or 0) or None)
    manifest = release.make_manifest(ROOT, archive, sha, version)
    doc = release.signed(pem, release.KIND_MANIFEST, manifest)
    target = out / release.manifest_name(version)
    target.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{archive.name}  ({archive.stat().st_size / 1e6:.1f} MB)")
    print(f"SHA-256  {sha}")
    print(f"Manifest {target.name} (signiert, {len(manifest['changelog'])} Punkte aus dem README)")


def cmd_publish(a) -> None:
    pem = _key_path().read_bytes()
    src = Path(a.src).resolve()
    dest = Path(a.dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    version = a.version or release.repo_version(ROOT)
    names = (release.archive_name(version), release.archive_name(version) + ".sha256", release.manifest_name(version))
    for name in names:
        if not (src / name).exists():
            raise SystemExit(f"{src / name} fehlt – zuerst «build».")
    release.verify_manifest(signing.public_of(pem), json.loads((src / names[2]).read_text(encoding="utf-8")))
    if src != dest:
        for name in names:
            shutil.copy2(src / name, dest / name)
    index = release.build_index(dest, pem)
    tmp = dest / ".index.json.tmp"
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(dest / "index.json")
    print(f"Veröffentlicht in {dest}: {', '.join(names)} + index.json ({len(index['index']['releases'])} Versionen)")
    if a.rsync:
        # zuerst die Dateien, zuletzt der Index: die Übersicht sieht eine Version erst, wenn alles da ist
        subprocess.run(["rsync", "-t", "--chmod=F644", *[str(dest / n) for n in names], a.rsync.rstrip("/") + "/"], check=True)
        subprocess.run(["rsync", "-t", "--chmod=F644", str(dest / "index.json"), a.rsync.rstrip("/") + "/"], check=True)
        print(f"Hochgeladen nach {a.rsync}")


def cmd_verify(a) -> None:
    try:
        doc = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Manifest nicht lesbar: {a.manifest} ({exc.__class__.__name__})")
    pub = a.pubkey or (signing.public_of(_key_path().read_bytes()) if os.environ.get("NFIZYO_RELEASE_KEY") else "")
    if not pub:
        raise SystemExit("--pubkey angeben")
    try:
        m = release.verify_manifest(pub, doc)
    except release.ReleaseError as exc:
        raise SystemExit(f"Manifest abgelehnt: {exc}")
    archive = Path(a.manifest).parent / m["file"]
    if archive.exists() and release.sha256_file(archive) != m["sha256"]:
        raise SystemExit("Prüfsumme des Archivs stimmt NICHT.")
    print(f"ok: {m['file']} Version {m['version']} ({'Archiv geprüft' if archive.exists() else 'Archiv nicht vorhanden'})")


def main() -> None:
    ap = argparse.ArgumentParser(prog="tools/release.py", description="N-Fizyo-Release bauen, signieren, veröffentlichen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen")
    sub.add_parser("pubkey")
    b = sub.add_parser("build")
    b.add_argument("--out", default=str(ROOT / "dist"))
    p = sub.add_parser("publish")
    p.add_argument("--dir", required=True, help="Release-Ordner (wird statisch ausgeliefert, z. B. /srv/n-fizyo-releases)")
    p.add_argument("--src", default=str(ROOT / "dist"))
    p.add_argument("--version", default=None)
    p.add_argument("--rsync", default=None, help="zusätzlich hochladen: benutzer@host:/srv/n-fizyo-releases")
    v = sub.add_parser("verify")
    v.add_argument("manifest")
    v.add_argument("--pubkey", default=None)
    a = ap.parse_args()
    try:
        {"keygen": cmd_keygen, "pubkey": cmd_pubkey, "build": cmd_build, "publish": cmd_publish, "verify": cmd_verify}[a.cmd](a)
    except release.ReleaseError as exc:          # klare Meldung statt Traceback, Rückgabewert ≠ 0
        raise SystemExit(f"Abgebrochen: {exc}")
    except (ValueError, TypeError) as exc:       # z. B. ungültiger öffentlicher Schlüssel (--pubkey)
        raise SystemExit(f"Abgebrochen: {exc.__class__.__name__}: {exc}")


if __name__ == "__main__":
    main()
