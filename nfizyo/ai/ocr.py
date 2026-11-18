"""Text aus Dokumenten lesen: PDF-Text (pypdf), sonst Texterkennung (Tesseract, lokal).

Tesseract ist optional (deploy/install.sh installiert es mit den Sprachpaketen deu/fra/ita/eng).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)
MIN_CHARS_PER_PAGE = 40


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _tesseract_langs() -> str:
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return "eng"
    have = {line.strip() for line in out.splitlines()}
    wanted = [l for l in ("deu", "fra", "ita", "eng") if l in have]
    return "+".join(wanted) or "eng"


def _ocr_image(path: Path) -> str:
    result = subprocess.run(
        ["tesseract", str(path), "stdout", "-l", _tesseract_langs(), "--psm", "6"],
        capture_output=True, text=True, timeout=300,
    )
    return result.stdout


# PDFs kommen von aussen (HIN-Post, Scanner, Upload). Ein präpariertes PDF darf die Box nicht lahmlegen:
# pypdf läuft deshalb in einem eigenen kurzen Prozess mit Zeit- und Speichergrenze (pypdf ≥ 6.21.4, requirements.txt).
PDF_TIMEOUT_SECONDS = 60
PDF_CPU_SECONDS = 45
PDF_MEMORY_BYTES = 1024 * 1024 * 1024     # 1 GiB Adressraum
PDF_MAX_CHARS = 2_000_000
_PDF_CHILD = r"""
import json, resource, sys
cpu, mem, max_chars = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
try:
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
except (ValueError, OSError):
    pass
from pypdf import PdfReader
reader = PdfReader(sys.argv[1])
pages, total = [], 0
for page in reader.pages:
    text = page.extract_text() or ""
    pages.append(text)
    total += len(text)
    if total > max_chars:
        break
sys.stdout.write(json.dumps({"pages": pages, "n": len(reader.pages)}))
"""


def _pdf_text(path: Path) -> tuple[str, int]:
    """Text aus einem PDF (pypdf) – in einem eigenen Prozess mit Zeit- und Speichergrenze. Fehler → ("", 0)."""
    import json
    import sys

    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", _PDF_CHILD, str(path), str(PDF_CPU_SECONDS), str(PDF_MEMORY_BYTES), str(PDF_MAX_CHARS)],
            capture_output=True, timeout=PDF_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        log.warning("PDF-Text: Zeitlimit überschritten (%s s) – %s", PDF_TIMEOUT_SECONDS, path.name)
        return "", 0
    except OSError:
        log.exception("PDF-Text: Prozess nicht gestartet")
        return "", 0
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace").strip().splitlines()
        log.warning("PDF konnte nicht gelesen werden (%s): %s", path.name, err[-1][:200] if err else f"Code {result.returncode}")
        return "", 0
    try:
        data = json.loads(result.stdout.decode("utf-8"))
        pages = [str(t) for t in data["pages"]]
        return "\n".join(pages)[:PDF_MAX_CHARS], int(data["n"])
    except (ValueError, KeyError, TypeError):
        return "", 0


def _pdf_ocr(path: Path) -> str:
    if not (tesseract_available() and shutil.which("pdftoppm")):
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-r", "300", "-png", str(path), f"{tmp}/p"], check=False, timeout=600)
        texts = [_ocr_image(img) for img in sorted(Path(tmp).glob("p*.png"))]
    return "\n".join(texts)


def extract_text(path: Path, mime: str | None = None) -> tuple[str, str]:
    """(Text, Methode) – Methode: text | pdf | ocr | none"""
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md") or (mime or "").startswith("text/"):
        return path.read_text(encoding="utf-8", errors="replace"), "text"
    if suffix == ".pdf" or mime == "application/pdf":
        text, pages = _pdf_text(path)
        if pages and len(text.strip()) >= MIN_CHARS_PER_PAGE * pages:
            return text, "pdf"
        ocr = _pdf_ocr(path)
        if ocr.strip():
            return ocr, "ocr"
        return text, "pdf" if text.strip() else "none"
    if suffix in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp") or (mime or "").startswith("image/"):
        if tesseract_available():
            return _ocr_image(path), "ocr"
        return "", "none"
    return "", "none"
