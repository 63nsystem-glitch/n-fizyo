"""Programmversion für Module (die das Paket selbst nicht importieren dürfen)."""
from __future__ import annotations


def app_version() -> str:
    from .. import __version__

    return __version__
