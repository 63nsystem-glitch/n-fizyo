"""Gemeinsame Seitenrahmen, die mehrere Module nutzen.

settings_page(): Einstellungen der Leitung. Jedes Modul bringt seine eigenen Bereiche mit
(Steckplatz «leitung.settings»: {"key", "label_key", "path", "order"}), z. B. die Agenda «Standorte»,
«Behandlungsarten», «Arbeitszeiten». Die Reiter oben zeigen alle Bereiche der eingeschalteten Module.
Vorlagen erweitern «ui/settings.html» und füllen den Block «settings».
"""
from __future__ import annotations

from aiohttp import web

from .screens import prefix
from .slots import collect
from .web import render


async def settings_tabs(request: web.Request, active: str) -> list[dict]:
    base = f"{prefix(request)}/einstellungen"
    tabs = []
    for item in await collect(request, "leitung.settings"):
        s = item["ctx"]
        tabs.append({"key": s["key"], "label_key": s["label_key"], "href": f"{base}/{s['path']}", "active": s["key"] == active})
    return tabs


async def settings_page(request: web.Request, active: str, template: str, ctx: dict | None = None) -> web.Response:
    full = {"settings_tabs": await settings_tabs(request, active), "section": active}
    full.update(ctx or {})
    return await render(request, template, full)
