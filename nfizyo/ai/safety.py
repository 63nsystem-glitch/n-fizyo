"""Sicherheitsregeln der KI-Schicht."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

_LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa")


def is_local_url(url: str) -> bool:
    """True, wenn die Adresse im lokalen Netz liegt (Praxis-Box oder LAN)."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in {"localhost"} or host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def ensure_local_url(url: str, allow_remote: bool) -> None:
    if not allow_remote and not is_local_url(url):
        raise ValueError(
            f"KI-Dienst {url!r} liegt nicht im lokalen Netz. Patientendaten verlassen die Praxis nicht. "
            "(Nur für Tests: NFIZYO_ALLOW_REMOTE_AI=1)"
        )


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", ".").rstrip("0").rstrip(".") if "." in n.replace(",", ".") else n for n in _NUM.findall(text)}


def invented_numbers(source: str, output: str, allowed: set[str] | None = None) -> set[str]:
    """Zahlen, die in der KI-Ausgabe stehen, aber nicht im Diktat. Solche Ausgaben werden verworfen."""
    allowed = allowed or {"10", "0"}  # VAS x/10, Extension 0°
    return numbers_in(output) - numbers_in(source) - allowed
