"""Signierte Versionen (Releases) von N-Fizyo.

Ein Release besteht aus drei Dateien in der Ablage von NSystem (statisch ausgeliefert, NFIZYO_RELEASE_BASE):
  n-fizyo-<version>.tar.gz              Programm (ohne Tests, Entwicklungswerkzeuge, Daten, Geheimnisse)
  n-fizyo-<version>.tar.gz.sha256       Prüfsumme (sha256sum-Format)
  n-fizyo-<version>.manifest.json       {"manifest": {...}, "signature": "<Ed25519>"} – Version, Datum, Prüfsumme, Auszug aus
                                        dem README («Yeni (sürüm …)»), Modulversionen, letzte Migration pro Besitzer
  index.json                            alle Manifeste, ebenfalls signiert – liest die NSystem-Übersicht
Signiert wird mit dem Release-Schlüssel von NSystem (nur auf dem Rechner, der Releases baut; Pfad aus NFIZYO_RELEASE_KEY,
nie im Projektordner). Die Box prüft vor update.sh: Signatur des Manifests (NFIZYO_RELEASE_PUBKEY), Dateiname, Prüfsumme.
Werkzeug: tools/release.py (keygen, build, publish, verify).
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import signing

KIND_MANIFEST = "release-manifest"
KIND_INDEX = "release-index"
FORMAT = 1
# Was ins Paket kommt (oberste Ebene) – alles andere (tests/, tools/, .github/, data/, dist/ …) bleibt draussen
INCLUDE_TOP = ("nfizyo", "deploy", "docs", "third_party", "README.md", "pyproject.toml", "requirements.txt", "requirements.lock",
               "requirements-ai.txt", ".env.example")
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".git", "node_modules", ".venv", "venv"}
EXCLUDE_RX = re.compile(r"(\.py[cod]|\.sqlite3.*|\.db|\.db-wal|\.db-shm|\.key|\.pem|\.p12|\.gpg|\.log|\.swp|\.tmp|~)$|^\.DS_Store$"
                        r"|^\.env$|\.env$")
VERSION_RX = re.compile(r"[0-9][0-9A-Za-z.\-]{0,30}")


class ReleaseError(Exception):
    pass


def archive_name(version: str) -> str:
    return f"n-fizyo-{version}.tar.gz"


def manifest_name(version: str) -> str:
    return f"n-fizyo-{version}.manifest.json"


def manifest_for_file(file: str) -> str:
    if not file.endswith(".tar.gz"):
        raise ReleaseError("Dateiname")
    return file[: -len(".tar.gz")] + ".manifest.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def repo_version(repo: Path) -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (repo / "nfizyo" / "__init__.py").read_text(encoding="utf-8"))
    if not m or not VERSION_RX.fullmatch(m.group(1)):
        raise ReleaseError("Version in nfizyo/__init__.py nicht gefunden")
    return m.group(1)


def files_for_release(repo: Path) -> list[Path]:
    """Dateien des Pakets (relativ zum Projektordner), sortiert. Ohne Tests, Werkzeuge, Daten, Caches, Geheimnisse."""
    out: list[Path] = []
    for top in INCLUDE_TOP:
        p = repo / top
        if p.is_file():
            out.append(Path(top))
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                rel = f.relative_to(repo)
                if any(part in EXCLUDE_DIRS for part in rel.parts) or not f.is_file() or f.is_symlink():
                    continue
                if EXCLUDE_RX.search(f.name) and f.name != ".env.example":
                    continue
                out.append(rel)
    return sorted(set(out))


def build_archive(repo: Path, out_dir: Path, version: str | None = None, mtime: int | None = None) -> tuple[Path, str]:
    """Reproduzierbares Archiv n-fizyo-<version>.tar.gz (feste Zeit, Besitzer root, Rechte 644/755). → (Pfad, sha256)."""
    version = version or repo_version(repo)
    mtime = int(mtime if mtime is not None else time.time())
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / archive_name(version)
    raw = io.BytesIO()
    root = f"n-fizyo-{version}"
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        dirs = set()
        for rel in files_for_release(repo):
            for parent in reversed(rel.parents[:-1]):
                dirs.add(parent)
        for d in sorted(dirs):
            info = tarfile.TarInfo(f"{root}/{d.as_posix()}")
            info.type, info.mode, info.mtime, info.uid, info.gid, info.uname, info.gname = tarfile.DIRTYPE, 0o755, mtime, 0, 0, "root", "root"
            tar.addfile(info)
        for rel in files_for_release(repo):
            src = repo / rel
            info = tarfile.TarInfo(f"{root}/{rel.as_posix()}")
            data = src.read_bytes()
            info.size, info.mtime, info.uid, info.gid, info.uname, info.gname = len(data), mtime, 0, 0, "root", "root"
            info.mode = 0o755 if src.stat().st_mode & 0o111 else 0o644
            tar.addfile(info, io.BytesIO(data))
    with open(target, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0, filename="") as gz:
        gz.write(raw.getvalue())
    sha = sha256_file(target)
    (out_dir / f"{target.name}.sha256").write_text(f"{sha}  {target.name}\n")
    return target, sha


def changelog_excerpt(readme: str, version: str, limit: int = 12) -> list[str]:
    """Punkte aus dem README-Abschnitt «Yeni (sürüm <version> …)»: fetter Titel + Anfang des Satzes."""
    start = re.search(rf"^Yeni \(sürüm {re.escape(version)}\b.*$", readme, re.M)
    if not start:
        return []
    rest = readme[start.end():]
    stop = re.search(r"^(Yeni \(sürüm|Önceki sürüm|Ayrıca \(sürüm|#)", rest, re.M)
    block = rest[: stop.start()] if stop else rest
    items, current = [], None
    for line in block.splitlines():
        if line.startswith("- "):
            if current:
                items.append(current)
            current = line[2:].strip()
        elif current is not None and line.startswith("  ") and line.strip():
            current += " " + line.strip()
    if current:
        items.append(current)
    out = []
    for it in items[:limit]:
        text = re.sub(r"`([^`]*)`", r"\1", it).replace("**", "")
        out.append(text if len(text) <= 180 else text[:177].rsplit(" ", 1)[0] + " …")
    return out


def migration_info(repo: Path) -> dict[str, str]:
    """Letzte Migration pro Besitzer (core, Module, central) – zeigt, ob ein Update die Datenbank ändert."""
    out = {}
    pkg = repo / "nfizyo"
    for d in sorted([pkg / "core" / "migrations", pkg / "central" / "migrations", *(pkg / "modules").glob("*/migrations")]):
        files = sorted(d.glob("[0-9][0-9][0-9]_*.sql")) if d.is_dir() else []
        if files:
            owner = d.parent.name
            out[owner] = files[-1].name
    return out


def module_versions(repo: Path) -> dict[str, str]:
    out = {}
    for init in sorted((repo / "nfizyo" / "modules").glob("*/__init__.py")):
        m = re.search(r'version="([^"]+)"', init.read_text(encoding="utf-8"))
        if m:
            out[init.parent.name] = m.group(1)
    return out


def make_manifest(repo: Path, archive: Path, sha: str, version: str, built_at: str | None = None) -> dict:
    readme = (repo / "README.md").read_text(encoding="utf-8") if (repo / "README.md").exists() else ""
    return {"format": FORMAT, "name": "n-fizyo", "version": version, "file": archive.name, "sha256": sha,
            "size": archive.stat().st_size,
            "built_at": built_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "changelog": changelog_excerpt(readme, version), "modules": module_versions(repo), "migrations": migration_info(repo)}


def signed(private_pem: bytes, kind: str, data: dict) -> dict:
    return {"manifest" if kind == KIND_MANIFEST else "index": data, "signature": signing.sign_payload(private_pem, kind, data),
            "key": signing.public_of(private_pem)[:12]}


def verify_manifest(public_b64: str, doc: dict) -> dict:
    """Manifest prüfen (Signatur, Felder). Gibt das Manifest zurück oder ReleaseError."""
    if not isinstance(doc, dict) or not isinstance(doc.get("manifest"), dict):
        raise ReleaseError("Manifest unlesbar")
    m = doc["manifest"]
    if not signing.verify_payload(public_b64, KIND_MANIFEST, m, str(doc.get("signature") or "")):
        raise ReleaseError("Signatur des Manifests ungültig")
    if m.get("format") != FORMAT or not VERSION_RX.fullmatch(str(m.get("version") or "")) \
            or m.get("file") != archive_name(str(m.get("version"))) or not re.fullmatch(r"[0-9a-f]{64}", str(m.get("sha256") or "")):
        raise ReleaseError("Manifest unvollständig")
    return m


def verify_index(public_b64: str, doc: dict) -> list[dict]:
    if not isinstance(doc, dict) or not isinstance(doc.get("index"), dict):
        raise ReleaseError("Index unlesbar")
    idx = doc["index"]
    if not signing.verify_payload(public_b64, KIND_INDEX, idx, str(doc.get("signature") or "")):
        raise ReleaseError("Signatur des Index ungültig")
    out = []
    for item in idx.get("releases") or []:
        try:
            out.append(verify_manifest(public_b64, item))
        except ReleaseError:
            continue
    return sorted(out, key=lambda m: version_key(m["version"]), reverse=True)


def version_key(v: str) -> tuple:
    """Vergleichbarer Schlüssel: 0.9 == 0.9.0 < 0.9.1-rc1 < 0.9.1 < 0.10.0 (Vorabversion «-…» vor der fertigen)."""
    main, _, pre = str(v).partition("-")
    nums = [int(p) if p.isdigit() else -1 for p in main.split(".")][:6]
    nums += [0] * (6 - len(nums))
    if not pre:
        return (*nums, 1, ())
    return (*nums, 0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[.\-]", pre)))


def build_index(release_dir: Path, private_pem: bytes) -> dict:
    """index.json aus allen Manifesten im Ordner (jedes wird mit dem eigenen öffentlichen Schlüssel geprüft)."""
    pub = signing.public_of(private_pem)
    items = []
    for f in sorted(release_dir.glob("n-fizyo-*.manifest.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        m = verify_manifest(pub, doc)
        if not (release_dir / m["file"]).exists() or sha256_file(release_dir / m["file"]) != m["sha256"]:
            raise ReleaseError(f"{m['file']} fehlt oder Prüfsumme stimmt nicht")
        items.append(doc)
    items.sort(key=lambda d: version_key(d["manifest"]["version"]), reverse=True)
    index = {"format": FORMAT, "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "releases": items}
    return signed(private_pem, KIND_INDEX, index)
