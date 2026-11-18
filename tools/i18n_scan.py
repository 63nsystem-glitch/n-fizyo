"""Findet Übersetzungsschlüssel, die im Code benutzt werden, aber in de.json fehlen.

python tools/i18n_scan.py [modul]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "nfizyo"
sys.path.insert(0, str(ROOT.parent))

from nfizyo.core.i18n import Translations  # noqa: E402

PATTERNS = [
    re.compile(r"""\bt\(\s*['"]([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)['"]"""),
    re.compile(r"""label_key\s*=?\s*['"]([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)['"]"""),
    re.compile(r"""NavItem\(\s*['"]([a-z][a-z0-9_.]+)['"]"""),
    re.compile(r"""\.t\([^,]+,\s*['"]([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)['"]"""),
    re.compile(r"""redirect_with_flash\([^,]+,\s*['"]([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)['"]"""),
    re.compile(r"""\(\s*['"][a-z_]+['"]\s*,\s*['"]([a-z]+\.[a-z0-9_.]+)['"]\s*,\s*['"][a-z_]+['"]\s*,"""),
]


def used_keys(only: str | None = None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for f in list(ROOT.rglob("*.html")) + list(ROOT.rglob("*.py")):
        if only and f"/{only}/" not in str(f):
            continue
        text = f.read_text(encoding="utf-8")
        for rx in PATTERNS:
            for m in rx.finditer(text):
                out.setdefault(m.group(1), set()).add(str(f.relative_to(ROOT)))
    return out


def main() -> None:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    tr = Translations()
    dirs = [ROOT / "core" / "locales"] + [p / "locales" for p in (ROOT / "modules").iterdir() if p.is_dir()]
    tr.load_dirs(dirs)
    missing = {}
    for key, files in sorted(used_keys(only).items()):
        if key not in tr.catalog["de"]:
            missing[key] = files
    for key, files in missing.items():
        print(f"{key:55s} {', '.join(sorted(files))[:90]}")
    print(f"\n{len(missing)} fehlende Schlüssel (de)")
    gaps = tr.missing()
    for lang, keys in gaps.items():
        if keys:
            print(f"{lang}: {len(keys)} fehlen, z. B. {keys[:5]}")


if __name__ == "__main__":
    main()
