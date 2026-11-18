"""Anatomie-Atlas und Struktur-Blatt (Seitenblatt in der Akte). Nur im Therapie-Bildschirm."""
from __future__ import annotations

import asyncio

from aiohttp import web

from ...core import languages
from ...core.keys import AI, DB
from ...core.tenant import current_practice_id
from ...core.modules import ModuleContext, NavItem
from ...core.web import render
from ...knowledge import anatomy
from ...knowledge.anatomy_data import STRUCTURES

GROUPS = ("head", "neck", "shoulder", "arm", "elbow", "hand", "chest", "abdomen", "back", "spine", "pelvis", "hip",
          "thigh", "knee", "leg", "foot")


def structure_context(structure, side: str | None, lang: str) -> dict:
    marks_m = {r: "hl-1" for r in anatomy.muscle_regions(structure, side)}
    marks_s = {b: "hl-1" for b in anatomy.skeleton_ids(structure, side)}
    related = [anatomy.get(r) for r in structure.related if anatomy.get(r)]
    # Strukturen mit gleichem Gelenk (z. B. alle Kniebänder) als Nachbarn
    if not related and structure.joint:
        related = [s for s in STRUCTURES if s.joint == structure.joint and s.id != structure.id][:8]
    # Sonst: Knochen eines Gelenks bzw. Gelenke eines Knochens (Blatt zeigt nicht nur den Namen)
    if not related and structure.bones and structure.type in ("joint", "bone"):
        want = "bone" if structure.type == "joint" else "joint"
        related = [s for s in STRUCTURES if s.type == want and s.id != structure.id and set(s.bones) & set(structure.bones)][:8]
    return {
        "s": structure,
        "side": side,
        "marks": {"muscle": marks_m, "skeleton": marks_s},
        "layer": "muscle" if structure.type in ("muscle", "group") else "skeleton",
        "view": "back" if any(r.startswith(("hamstring", "gluteal", "lower_back", "upper_back", "trapezius", "back_deltoids",
                                            "soleus", "triceps")) for r in marks_m) and structure.type in ("muscle", "group") else "front",
        "related": related,
        "type_name": anatomy.type_name(structure, lang),
        "place": anatomy.place_name(structure, side, lang),
    }


async def atlas(request: web.Request) -> web.StreamResponse:
    lang = request["lang"]
    q = request.query.get("q", "").strip()
    group = request.query.get("g", "")
    items = anatomy.search(q, limit=200) if q else list(STRUCTURES)
    if group:
        items = [s for s in items if s.group == group]
    items.sort(key=lambda s: (GROUPS.index(s.group) if s.group in GROUPS else 99, anatomy.name(s, lang)))
    selected = anatomy.get(request.query.get("s"))
    if selected is None and request.query.get("bone"):
        selected, _ = anatomy.bone_structure(request.query["bone"])
    ctx = {"items": items, "q": q, "group": group, "groups": GROUPS, "selected": selected}
    if selected:
        ctx.update(structure_context(selected, request.query.get("side"), lang))
    return await render(request, "anatomy/atlas.html", ctx)


async def sheet(request: web.Request) -> web.StreamResponse:
    structure = anatomy.get(request.match_info["sid"])
    if structure is None:
        raise web.HTTPNotFound()
    side = request.query.get("side") if request.query.get("side") in ("l", "r", "b") else None
    lang = request["lang"]
    ctx = structure_context(structure, side, lang)
    # Name in der Sprache der Patientin / des Patienten (für Erklärungen), falls vorhanden
    patient_term = None
    pid = request.query.get("patient", "")
    if pid.isdigit():
        db = request.app[DB]
        from ...core import access

        patient = None
        if access.relation(db, request["user"]["id"], int(pid)):
            patient = db.one("SELECT spoken_lang, lang FROM patients WHERE id = ? AND practice_id = ?",
                             (int(pid), current_practice_id()))
        target = patient and (patient["spoken_lang"] or patient["lang"])
        if target and target not in ("de", "fr", "gsw"):
            result = await asyncio.to_thread(
                request.app[AI].translate, db, current_practice_id(), anatomy.name(structure, "de"), "de", target
            )
            if result.text and result.engine != "identity":
                patient_term = {"text": result.text, "lang": languages.name_for(target, lang), "engine": result.engine}
    ctx["patient_term"] = patient_term
    return await render(request, "anatomy/sheet.html", ctx)


def setup(ctx: ModuleContext) -> None:
    ctx.page("therapie", "/anatomie", atlas)
    ctx.page("therapie", "/anatomie/{sid}/blatt", sheet)
    ctx.nav("therapie", NavItem("nav.anatomy", "skeleton", "/anatomie", order=85))
