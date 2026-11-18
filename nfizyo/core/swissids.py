"""Schweizer Kennnummern so, wie sie auf Briefen und Karten stehen, in die Form der elektronischen Rechnung bringen.

GLN: 13 Ziffern («7601 0000 0000 1» → «7601000000001»). ZSR: ein Buchstabe und 6 Ziffern («H 1234.56», «h123456» → «H123456»).
Ungültiges bleibt None – lieber ein klarer Hinweis «fehlt» als ein XML, das MediData ablehnt.
"""
from __future__ import annotations

import re


def clean_gln(value: str | None) -> str | None:
    v = re.sub(r"[\s.\-]", "", value or "")
    return v if re.fullmatch(r"\d{13}", v) else None


def clean_zsr(value: str | None) -> str | None:
    v = re.sub(r"[\s.\-]", "", value or "").upper()
    return v if re.fullmatch(r"[A-Z]\d{6}", v) else None
