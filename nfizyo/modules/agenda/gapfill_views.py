"""Lücke füllen – Empfang bietet einen frei gewordenen Termin per SMS an; die Person antwortet über einen Link (ohne Anmeldung)."""
from __future__ import annotations

from aiohttp import web

from ...core.i18n import PATIENT_LANGS
from ...core.keys import DB, I18N, SMS, THROTTLE
from ...core.screens import prefix
from ...core.tenant import current_practice_id, practice_scope
from ...core.timeutil import fmt_date, fmt_time, from_iso
from ...core.web import LANG_COOKIE, client_ip, public_url, redirect_with_flash, render
from . import gapfill


def _when(start_utc: str, lang: str) -> str:
    s = from_iso(start_utc).astimezone(gapfill.TZ)
    return f"{fmt_date(s, lang, weekday=True, year=False)} {fmt_time(s)}"


def _practice(db, practice_id: int):
    return db.one("SELECT id, name, phone, address FROM practices WHERE id = ?", (practice_id,))


def _place(db, location_id: int | None, practice) -> str:
    if location_id:
        loc = db.one("SELECT name, address FROM locations WHERE id = ?", (location_id,))
        if loc:
            return loc["name"]
    return practice["name"]


def _sms(request: web.Request, practice, phone: str, lang: str, key: str, *, secrets=(), **params) -> None:
    text = request.app[I18N].t(lang, f"p.sms.{key}", practice=practice["name"], phone=practice["phone"] or "", **params)
    request.app[SMS].send(practice["id"], "sms", phone, text, secrets=secrets)


# --------------------------------------------------------------------------- Empfang: Karte auf dem abgesagten Termin
def card(request: web.Request, a) -> dict | None:
    """Kontext für «Lücke füllen» (nur Empfang, nur abgesagte Termine mit Patient/in)."""
    if a["status"] != "cancelled" or not a["patient_id"]:
        return None
    db = request.app[DB]
    offer = gapfill.current_offer(db, a["id"])
    recs = gapfill.recipients(db, offer["id"]) if offer else []
    is_open = gapfill.gap_open(db, a)
    taken = offer is not None and offer["status"] == "taken"
    cands = gapfill.candidates(db, a) if is_open and not taken else {"sms": [], "call": []}
    contacted = {r["waitlist_id"] for r in recs} if offer and offer["status"] == "open" else set()
    cands["sms"] = [c for c in cands["sms"] if c.entry["id"] not in contacted]
    practice = _practice(db, current_practice_id())
    when = _when(a["starts_at"], request["lang"])
    preview = request.app[I18N].t(request["lang"], "p.sms.offer", practice=practice["name"], when=when,
                                  place=_place(db, a["location_id"], practice), url="https://…/p/angebot/…")
    accepted = next((r for r in recs if r["status"] == "accepted"), None)
    start = from_iso(a["starts_at"])
    off = gapfill.off_time(db, a) if not is_open and not taken else []
    return {"offer": offer, "recipients": recs, "cands": cands, "is_open": is_open, "taken": taken, "accepted": accepted, "off": off,
            "preview": preview, "past": start <= gapfill.now_utc(), "expires": from_iso(offer["expires_at"]) if offer else None,
            "preselect": [c.entry["id"] for c in cands["sms"] if c.fits and not {"rx_none", "rx_used"} & set(c.warnings)][:3]}


async def offer_post(request: web.Request) -> web.StreamResponse:
    from .views import load_appointment

    db = request.app[DB]
    a = load_appointment(request)
    back = f"{prefix(request)}/agenda/termine/{a['id']}"
    form = await request.post()
    practice = _practice(db, current_practice_id())
    when_of = lambda lang: _when(a["starts_at"], lang)  # noqa: E731
    if form.get("action") == "close":
        offer = gapfill.current_offer(db, a["id"])
        if offer is None:
            raise web.HTTPNotFound()
        for r in gapfill.close_offer(db, offer["id"]):
            _sms(request, practice, r["phone"], r["lang"], "offer_gone", when=when_of(r["lang"]))
        raise redirect_with_flash(back, "gap.closed")
    if a["status"] != "cancelled" or not gapfill.gap_open(db, a):
        raise redirect_with_flash(back, "gap.gone")
    chosen = {int(x) for x in form.getall("w", []) if str(x).isdigit() and len(str(x)) <= 9}
    cands = {c.entry["id"]: c for c in gapfill.candidates(db, a)["sms"]}
    picked = [cands[w] for w in chosen if w in cands]
    if not picked:
        raise redirect_with_flash(back, "gap.choose")
    offer = gapfill.current_offer(db, a["id"])
    with db.tx():
        oid = offer["id"] if offer is not None and offer["status"] == "open" else gapfill.create_offer(db, a)
        sent = []
        for c in picked:
            token = gapfill.add_recipient(db, oid, c.entry)
            if token:
                sent.append((c.entry, token))
    base = public_url(request)
    place = _place(db, a["location_id"], practice)
    for entry, token in sent:
        lang = entry["patient_lang"] or "de"
        _sms(request, practice, entry["patient_phone"] or entry["phone"], lang, "offer", when=when_of(lang), place=place,
             url=f"{base}/p/angebot/{token}", secrets=(token,))
    raise redirect_with_flash(back, "gap.sent" if len(sent) != 1 else "gap.sent_one")


# --------------------------------------------------------------------------- Patient/in: Antwort über den Link
def _lang(request: web.Request, rec) -> str:
    chosen = request.cookies.get(LANG_COOKIE)
    return chosen if chosen in PATIENT_LANGS else (rec["lang"] if rec["lang"] in PATIENT_LANGS else request["lang"])


async def public_offer(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    throttle = request.app[THROTTLE]
    key = f"offer:{client_ip(request)}"
    if throttle.blocked(key):
        return await render(request, "agenda/p_offer.html", {"state": "blocked", "practice": None}, status=429)
    rec = gapfill.by_token(db, request.match_info["token"])
    if rec is None:
        throttle.fail(key)
        return await render(request, "agenda/p_offer.html", {"state": "invalid", "practice": None}, status=404)
    request["lang"] = _lang(request, rec)
    lang = request["lang"]
    practice = _practice(db, rec["practice_id"])
    with practice_scope(rec["practice_id"]):
        gapfill.expire_due(db)
        rec = gapfill.by_token(db, request.match_info["token"])
        state = {"open": "open", "taken": "taken", "closed": "taken", "expired": "expired"}[rec["offer_status"]]
        if rec["status"] == "accepted":
            state = "booked"
        elif rec["status"] == "declined" and state == "open":
            state = "declined"
        if request.method == "POST":
            form = await request.post()
            if form.get("answer") == "yes" and state in ("open", "declined"):
                result, aid, others = gapfill.accept(db, rec)
                if result == "booked" and aid and rec["status"] != "accepted":
                    from .views import booked

                    await booked(request, aid, rec["patient_id"], rec["therapist_id"])
                    _sms(request, practice, rec["phone"], lang, "offer_booked", when=_when(rec["starts_at"], lang))
                for r in others:
                    _sms(request, practice, r["phone"], r["lang"], "offer_gone", when=_when(rec["starts_at"], r["lang"]))
            elif form.get("answer") == "no" and state == "open":
                gapfill.decline(db, rec)
            raise web.HTTPFound(request.path)
        place = _place(db, rec["location_id"], practice)
        therapist = db.value("SELECT name FROM users WHERE id = ?", (rec["therapist_id"],))
    start, end = from_iso(rec["starts_at"]).astimezone(gapfill.TZ), from_iso(rec["ends_at"]).astimezone(gapfill.TZ)
    return await render(request, "agenda/p_offer.html", {
        "state": state, "practice": practice, "start": start, "minutes": int((end - start).total_seconds() // 60),
        "place": place, "therapist": therapist, "path": request.path,
    })


def setup(ctx) -> None:
    ctx.page("empfang", "/agenda/termine/{aid:\\d+}/luecke", offer_post, ("POST",))
    ctx.public("/p/angebot/{token:[A-Za-z0-9_-]{20,100}}", public_offer, ("GET", "POST"))
