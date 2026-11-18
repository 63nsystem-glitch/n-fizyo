"""Kasse (Empfang und Leitung).

/kasse                      Verkauf (Produkte, Abos, Leistungen) – bar, Twint, Karte oder auf Rechnung; offene Rechnung bezahlen
/kasse?ansicht=heute        Quittungen des Tages
/kasse?ansicht=abos         Laufende Abos: Besuch abhaken
/kasse?ansicht=abschluss    Tagesabschluss (Bargeld zählen) und frühere Abschlüsse
/kasse/belege/{id}          Quittung (Storno vor dem Abschluss), PDF
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

from aiohttp import web

from ...core import money
from ...core import patients as core_patients
from ...core.audit import audit
from ...core.keys import DB, I18N, MAIL, SETTINGS
from ...core.modules import ModuleContext, NavItem
from ...core.screens import prefix
from ...core.tenant import current_practice_id
from ...core.timeutil import today
from ...core.web import int_param, redirect_with_flash, render
from ..billing import api as billing_api
from . import repo


def _float(v, hi: float = 1_000_000) -> float | None:
    """Betrag/Anzahl aus dem Formular – None bei Unsinn (leer, keine Zahl, unendlich, riesig, negativ): Formularfehler statt 500."""
    return money.parse(v, lo=0.0, hi=hi)


def _day(value: str | None) -> str:
    try:
        d = date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return today().isoformat()
    return min(d, today()).isoformat()


def _id(value) -> int | None:
    """ID aus einem Formular/Link – None bei Unsinn (auch 5000 Ziffern: int() hätte eine Ausnahme)."""
    v = str(value or "").strip()
    return int(v) if v.isdigit() and len(v) <= 12 else None


def _patient(db, value) -> dict | None:
    pid = _id(value)
    return core_patients.get(db, pid) if pid else None


# --------------------------------------------------------------------------- Kasse
async def desk(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    if request.method == "POST":
        form = await request.post()
        do = form.get("do")
        if do in ("sell", "preview"):
            return await _sell(request, form, preview=(do == "preview"))
        if do == "pay_invoice":
            return await _pay_invoice(request, form)
        if do in ("visit", "visit_undo") and _id(form.get("sub_id")):
            sub = repo.subscription(db, _id(form.get("sub_id")), today().isoformat())
            if sub is None:
                raise web.HTTPNotFound()
            ok = repo.add_visit(db, sub, today().isoformat()) if do == "visit" else repo.remove_visit(db, sub, today().isoformat())
            back = str(form.get("back") or "")
            target = back if back.startswith(f"{S}/") and "//" not in back else f"{S}/kasse?ansicht=abos"
            raise redirect_with_flash(target, ("cashdesk.abo.visited" if do == "visit" else "cashdesk.abo.undone") if ok else "cashdesk.abo.not_possible")
        if do == "close":
            day = _day(form.get("day"))
            counted = _float(form.get("counted"))
            if counted is None or counted < 0 or counted > 1_000_000:
                raise redirect_with_flash(f"{S}/kasse?ansicht=abschluss&tag={day}", "cashdesk.err.counted")
            if repo.close_day(db, day, counted, str(form.get("note") or "").strip()) is None:
                raise redirect_with_flash(f"{S}/kasse?ansicht=abschluss&tag={day}", "cashdesk.close.already")
            raise redirect_with_flash(f"{S}/kasse?ansicht=abschluss&tag={day}", "cashdesk.close.done")
        raise redirect_with_flash(f"{S}/kasse", "cashdesk.err.invalid")
    tab = request.query.get("ansicht", "verkauf")
    return await _render_desk(request, tab)


async def _render_desk(request: web.Request, tab: str, *, values: dict | None = None, errors: list[str] | None = None,
                       status: int = 200, totals: dict | None = None) -> web.StreamResponse:
    db = request.app[DB]
    day = today().isoformat()
    ctx: dict = {"tab": tab, "day": day, "leitung": request["screen"].key == "leitung", "errors": errors or [],
                 "values": {"qty": {}, "method": "", "given": "", "note": "", "patient_id": None, **(values or {})},
                 "methods": repo.METHODS, "vat_number": billing_api.vat_number(db), "totals": totals}
    if tab == "heute":
        sel = _day(request.query.get("tag"))
        ctx.update({"sel_day": sel, "sales": repo.day_sales(db, sel), "summary": repo.day_summary(db, sel),
                    "closed": repo.is_closed(db, sel), "prev_day": (date.fromisoformat(sel) - timedelta(days=1)).isoformat(),
                    "next_day": (date.fromisoformat(sel) + timedelta(days=1)).isoformat() if sel < day else None})
    elif tab == "abos":
        ctx["subs"] = repo.current_subscriptions(db, day)
    elif tab == "abschluss":
        sel = _day(request.query.get("tag"))
        ctx.update({"sel_day": sel, "summary": repo.day_summary(db, sel), "closing": repo.closing(db, sel),
                    "closings": repo.closings(db), "unclosed": repo.unclosed_days(db, day)})
    else:
        ctx["tab"] = "verkauf"
        q = request.query.get("q", "").strip()
        patient = _patient(db, request.query.get("patient") or ctx["values"]["patient_id"])
        ctx.update({"items": billing_api.price_items(db), "patient": patient, "q": q,
                    "found": core_patients.search(db, q, limit=8) if q and patient is None else [],
                    "subs": repo.subscriptions_for(db, patient["id"], day)[:3] if patient else [],
                    "invoice": billing_api.open_invoice_by_number(db, request.query.get("rechnung", "")) if request.query.get("rechnung") else None,
                    "invoice_q": request.query.get("rechnung", "")})
    return await render(request, "cashdesk/desk.html", ctx, status=status)


async def _sell(request: web.Request, form, *, preview: bool = False) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    patient = _patient(db, form.get("patient_id"))
    entries = []
    for it in billing_api.price_items(db):
        raw = str(form.get(f"qty_{it['id']}") or "").strip()
        if raw and raw != "0":
            q = _float(raw, hi=repo.MAX_QTY)
            entries.append((it, q if q is not None else -1))
    method = str(form.get("method") or "")
    given_raw = str(form.get("given") or "").strip()
    given = _float(given_raw) if given_raw else None
    values = {"patient_id": patient["id"] if patient else None, "method": method, "given": given_raw[:20],
              "qty": {it["id"]: q for it, q in entries}, "note": str(form.get("note") or "")[:200]}
    totals = repo.totals(db, entries, given if method == "cash" else None, method)
    if preview:                                     # «Total berechnen» ohne JavaScript: gleiche Rechnung wie beim Verkauf
        return await _render_desk(request, "verkauf", values=values, totals=totals)
    if given_raw and given is None and method == "cash":
        return await _render_desk(request, "verkauf", values=values, errors=["amount"], status=400, totals=totals)
    try:
        sid, invoice_id = repo.sell(db, patient=patient, entries=entries, method=method, lang=request["lang"], day=today().isoformat(),
                                    given=given, note=values["note"].strip() or None)
    except repo.SaleError as exc:
        return await _render_desk(request, "verkauf", values=values, errors=[exc.code], status=400, totals=totals)
    raise redirect_with_flash(f"{S}/kasse/belege/{sid}", "cashdesk.sold_invoice" if invoice_id else "cashdesk.sold")


async def _pay_invoice(request: web.Request, form) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    number = str(form.get("number") or "").strip()[:20]
    inv = billing_api.open_invoice_by_number(db, number)
    amount = _float(form.get("amount"))
    method = str(form.get("method") or "")
    back = f"{S}/kasse?rechnung={number}"
    if inv is None or amount is None or method not in billing_api.DESK_METHODS:
        raise redirect_with_flash(back, "cashdesk.err.pay")
    if repo.is_closed(db, today().isoformat()):
        raise redirect_with_flash(back, "cashdesk.err.closed")
    # höchstens der offene Betrag; bar auf 5 Rappen (die Rechnung ist dann bezahlt, die Rundung wird gebucht)
    if billing_api.pay_at_desk(db, inv["id"], amount, method, today().isoformat()) is None:
        raise redirect_with_flash(back, "cashdesk.err.pay")
    raise redirect_with_flash(f"{S}/kasse?ansicht=heute", "cashdesk.paid_invoice")


# --------------------------------------------------------------------------- Quittung
async def sale_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    sale = repo.get(db, int_param(request, "sid"))
    if sale is None:
        raise web.HTTPNotFound()
    if request.method == "POST":
        form = await request.post()
        if form.get("do") == "email":
            ok = await _email_receipt(request, sale)
            raise redirect_with_flash(f"{S}/kasse/belege/{sale['id']}", "cashdesk.mail_sent" if ok else "cashdesk.mail_not_allowed")
        sale = repo.get(db, sale["id"]) or sale                  # Stand nach dem Absenden (z. B. inzwischen storniert)
        if form.get("do") == "cancel":
            reason = str(form.get("reason") or "").strip()
            done = repo.cancel(db, sale, reason)
            if done == "invoice_final":
                raise redirect_with_flash(f"{S}/kasse/belege/{sale['id']}", "cashdesk.err.cancel_invoice_final")
            if done:
                raise redirect_with_flash(f"{S}/kasse/belege/{sale['id']}", "cashdesk.cancelled")
            raise redirect_with_flash(f"{S}/kasse/belege/{sale['id']}", "cashdesk.err.cancel")
        if form.get("do") in ("correct_storno", "correct_method") and request["screen"].key == "leitung":
            reason = str(form.get("reason") or "").strip()
            if form.get("do") == "correct_storno":
                cid = repo.correct_storno(db, sale, reason, refund_day=today().isoformat() if form.get("refund") == "1" else None)
            else:
                cid = repo.correct_method(db, sale, str(form.get("to_method") or ""), reason)
            raise redirect_with_flash(f"{S}/kasse/belege/{sale['id']}", "cashdesk.corr.saved" if cid else "cashdesk.corr.failed")
        raise web.HTTPFound(f"{S}/kasse/belege/{sale['id']}")
    lines = repo.lines(db, sale["id"])
    patient = core_patients.get(db, sale["patient_id"]) if sale["patient_id"] else None
    return await render(request, "cashdesk/sale.html", {
        "email_ok": core_patients.email_allowed(patient) and sale["status"] != "cancelled",
        "sale": sale, "lines": lines, "vat": billing_api.vat_summary(lines), "closed": repo.is_closed(db, sale["day"]),
        "items_total": money.rp(sale["total_chf"] - (sale["rounding_chf"] or 0)),
        "corrections": repo.corrections_for_sale(db, sale["id"]), "current_method": repo.current_method(db, sale),
        "today_closed": repo.is_closed(db, today().isoformat()), "paid_methods": repo.PAID_METHODS,
        "change": round(sale["given_chf"] - sale["total_chf"], 2) if sale["given_chf"] else None})


def _tl(request: web.Request, lang: str):
    i18n = request.app[I18N]
    return lambda key, **kw: i18n.t(lang, key, **kw)


def _receipt_bytes(request: web.Request, sale) -> bytes:
    from . import pdf as receipt_pdf

    db = request.app[DB]
    practice = db.one("SELECT * FROM practices WHERE id = ?", (current_practice_id(),))
    patient = core_patients.get(db, sale["patient_id"]) if sale["patient_id"] else None
    lang = repo.receipt_lang(db, patient)
    return receipt_pdf.build(sale=sale, lines=repo.lines(db, sale["id"]), practice=practice, tl=_tl(request, lang), lang=lang,
                             demo=bool(practice["is_demo"]) or request.app[SETTINGS].demo,
                             corrections=repo.corrections_for_sale(db, sale["id"]), current_method=repo.current_method(db, sale))


async def _email_receipt(request: web.Request, sale) -> bool:
    db = request.app[DB]
    patient = core_patients.get(db, sale["patient_id"]) if sale["patient_id"] else None
    if not core_patients.email_allowed(patient) or sale["status"] == "cancelled":
        return False
    practice = db.one("SELECT name, phone FROM practices WHERE id = ?", (current_practice_id(),))
    data = await asyncio.to_thread(_receipt_bytes, request, sale)
    i18n = request.app[I18N]
    lang = patient["lang"] or "de"
    params = {"name": patient["first_name"], "number": sale["number"], "practice": practice["name"], "phone": practice["phone"] or ""}
    body = i18n.t(lang, "p.mail.receipt_body", **params) + "\n\n" + i18n.t(lang, "p.mail.footer", **params)
    request.app[MAIL].send(current_practice_id(), patient["email"], i18n.t(lang, "p.mail.receipt_subject", **params), body,
                           attachment=(f"quittung-{sale['number']}.pdf", data))
    audit(db, "email", "cash_sale", sale["id"], patient["id"], {"number": sale["number"]})
    return True


async def sale_pdf(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    sale = repo.get(db, int_param(request, "sid"))
    if sale is None:
        raise web.HTTPNotFound()
    data = await asyncio.to_thread(_receipt_bytes, request, sale)
    audit(db, "export", "cash_sale", sale["id"], sale["patient_id"], {"format": "pdf"})
    return web.Response(body=data, content_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="quittung-{sale["number"]}.pdf"'})


# --------------------------------------------------------------------------- Steckplätze
async def card_patient(request: web.Request, patient) -> dict | None:
    db = request.app[DB]
    day = today().isoformat()
    subs = [s for s in repo.subscriptions_for(db, patient["id"], day) if s["state"] != "cancelled"][:3]
    sales = repo.for_patient(db, patient["id"], limit=3)
    return {"patient": patient, "subs": subs, "sales": sales}


def action_sell(request: web.Request, patient) -> dict:
    return {"label_key": "cashdesk.sell_btn", "icon": "point_of_sale", "href": f"{prefix(request)}/kasse?patient={patient['id']}"}


def work_unclosed(request: web.Request) -> dict:
    days = repo.unclosed_days(request.app[DB], today().isoformat())
    return {"label_key": "cashdesk.work_unclosed", "icon": "point_of_sale", "n": len(days),
            "href": f"{prefix(request)}/kasse?ansicht=abschluss" + (f"&tag={days[0]}" if days else "")}


def setup(ctx: ModuleContext) -> None:
    # Die Abrechnung bucht kein Bargeld/Twint/Karte auf einen abgeschlossenen Kassentag (Rechnungsseite, auch rückdatiert)
    ctx.set(billing_api.CASH_DAY_CLOSED, repo.is_closed)
    for screen, order in (("empfang", 45), ("leitung", 35)):
        ctx.page(screen, "/kasse", desk, ("GET", "POST"))
        ctx.page(screen, "/kasse/belege/{sid:\\d+}", sale_page, ("GET", "POST"))
        ctx.page(screen, "/kasse/belege/{sid:\\d+}/pdf", sale_pdf)
        ctx.nav(screen, NavItem("nav.cashdesk", "point_of_sale", "/kasse", order=order))
    ctx.slot("empfang.patient.card", 35, card_patient, "cashdesk/card_patient.html")
    ctx.slot("empfang.patient.action", 30, action_sell)
    ctx.slot("leitung.overview.work", 57, work_unclosed)
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('cash_sales', 'subscriptions'), (
        "UPDATE cash_sales SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "UPDATE subscriptions SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
