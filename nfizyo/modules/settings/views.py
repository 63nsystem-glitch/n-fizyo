"""Leitung: Team (Rollen, Zugang), Vertretungen, Einstellungen der Praxis (Praxis, Zuweisende, KI, Module).

Weitere Einstellungsbereiche bringen die Module selbst mit (Steckplatz «leitung.settings»), z. B. die Agenda
«Standorte», «Behandlungsarten», «Arbeitszeiten» und die Abrechnung «Tarife».
"""
from __future__ import annotations

import asyncio
import re
from datetime import date

from aiohttp import web

from ...core import access, mfa
from ...core import patients as core_patients
from ...core import practice as core_practice
from ...core.audit import audit
from ...core.auth import ROLES, create_user, normalize_roles, set_password, set_user_active, update_user
from ...core import sms as core_sms
from ...core.keys import AI, DB, HIN, I18N, MAIL, MODULES, SETTINGS, SMS
from ...core.modules import ModuleContext, NavItem, enabled, module_enabled, set_module_enabled
from ...core.pages import settings_page
from ...core.screens import prefix, roles_of
from ...core.security import new_token
from ...core.tenant import current_practice_id, current_user_id
from ...core.web import redirect_with_flash, render
from ...core.timeutil import today as zurich_today


def _s(form, key: str, maxlen: int = 300) -> str:
    return str(form.get(key) or "").strip()[:maxlen]


def _id(form, key: str = "id") -> int | None:
    v = str(form.get(key) or "")
    return int(v) if v.isdigit() else None


def _temp_password() -> str:
    """Lesbares Startpasswort (einmalig angezeigt)."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    raw = new_token(12)
    return "-".join("".join(alphabet[ord(c) % len(alphabet)] for c in raw[i:i + 4]) for i in (0, 4, 8))


def _owners_left(db, without: int) -> int:
    return sum(1 for u in db.query("SELECT id, roles FROM users WHERE practice_id = ? AND active = 1", (current_practice_id(),))
               if u["id"] != without and "owner" in roles_of(u))


# --------------------------------------------------------------------------- Team
async def team_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    pid = current_practice_id()
    back = f"{prefix(request)}/team"
    new_password = None
    if request.method == "POST":
        form = await request.post()
        do = form.get("do")
        if do == "add":
            email, name, lang = _s(form, "email", 200).lower(), _s(form, "name", 120), _s(form, "lang", 2)
            try:
                roles = normalize_roles(form.getall("role", []))
            except ValueError:
                roles = None
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or not name or not roles or lang not in ("de", "fr"):
                raise redirect_with_flash(back, "settings.invalid")
            if db.value("SELECT 1 FROM users WHERE email = ?", (email,)):
                raise redirect_with_flash(back, "settings.email_taken")
            gln = _ids(_s(form, "gln", 40), "", back)[0]   # vor dem Anlegen prüfen (sonst wäre das Startpasswort verloren)
            pw = _temp_password()
            uid = create_user(db, pid, email, name, roles, pw, lang)
            update_user(db, uid, roles=roles, gln=gln, zsr=None)
            audit(db, "create", "user", uid, None, {"roles": roles})
            new_password = {"name": name, "email": email, "password": pw}
        else:
            target = db.one("SELECT * FROM users WHERE id = ? AND practice_id = ?", (_id(form, "user_id") or 0, pid))
            if target is None:
                raise redirect_with_flash(back, "settings.invalid")
            if do == "update":
                try:
                    roles = normalize_roles(form.getall("role", []))
                except ValueError:
                    raise redirect_with_flash(back, "settings.invalid")
                if "owner" not in roles and "owner" in roles_of(target) and (target["id"] == current_user_id() or not _owners_left(db, target["id"])):
                    raise redirect_with_flash(back, "settings.keep_owner")
                gln, zsr = _ids(_s(form, "gln", 40), _s(form, "zsr", 20), back)
                update_user(db, target["id"], roles=roles, gln=gln, zsr=zsr)
                audit(db, "update", "user", target["id"], None, {"roles": roles})
                raise redirect_with_flash(back, "common.saved")
            if do in ("deactivate", "activate"):
                if target["id"] == current_user_id():
                    raise redirect_with_flash(back, "settings.not_self")
                if do == "deactivate" and "owner" in roles_of(target) and not _owners_left(db, target["id"]):
                    raise redirect_with_flash(back, "settings.keep_owner")
                set_user_active(db, target["id"], do == "activate")
                audit(db, do, "user", target["id"])
                raise redirect_with_flash(back, "common.saved")
            if do == "reset":
                if target["id"] == current_user_id():
                    raise redirect_with_flash(back, "settings.not_self")
                pw = _temp_password()
                set_password(db, target["id"], pw)
                audit(db, "password_reset", "user", target["id"])
                new_password = {"name": target["name"], "email": target["email"], "password": pw}
            elif do == "mfa_reset":
                if target["id"] == current_user_id():
                    raise redirect_with_flash(back, "settings.not_self")
                mfa.reset(db, target["id"])
                audit(db, "mfa_reset", "user", target["id"])
                raise redirect_with_flash(back, "mfa.reset_done")
            else:
                raise redirect_with_flash(back, "settings.invalid")
    users = [dict(u) | {"role_set": roles_of(u)} for u in
             db.query("SELECT * FROM users WHERE practice_id = ? ORDER BY active DESC, name", (pid,))]
    return await render(request, "settings/team.html", {"users": users, "roles": ROLES, "new_password": new_password})


# --------------------------------------------------------------------------- Vertretungen
async def delegations_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    back = f"{prefix(request)}/vertretungen"
    therapists = core_patients.therapists(db)
    ids = {t["id"] for t in therapists}
    if request.method == "POST":
        form = await request.post()
        if form.get("do") == "revoke" and _id(form):
            access.revoke(db, _id(form))
            raise redirect_with_flash(back, "settings.delegation_revoked")
        f, to = _id(form, "from_user"), _id(form, "to_user")
        try:
            starts, ends = date.fromisoformat(_s(form, "starts_on", 10)), date.fromisoformat(_s(form, "ends_on", 10))
        except ValueError:
            raise redirect_with_flash(back, "settings.invalid")
        if f not in ids or to not in ids or f == to or ends < starts:
            raise redirect_with_flash(back, "settings.invalid")
        access.delegate(db, f, to, starts.isoformat(), ends.isoformat(), _s(form, "note", 200) or None)
        raise redirect_with_flash(back, "settings.delegation_added")
    return await render(request, "settings/delegations.html", {
        "items": access.delegations(db), "therapists": therapists, "today": zurich_today().isoformat()})


def _ids(gln: str, zsr: str, back: str) -> tuple[str | None, str | None]:
    """GLN/ZSR wie auf dem Brief eingetippt («7601 0000 …», «H 1234.56») → Form der Rechnung; Ungültiges → klare Meldung."""
    from ...core.swissids import clean_gln, clean_zsr

    if gln and not clean_gln(gln):
        raise redirect_with_flash(back, "settings.bad_gln")
    if zsr and not clean_zsr(zsr):
        raise redirect_with_flash(back, "settings.bad_zsr")
    return clean_gln(gln), clean_zsr(zsr)


# --------------------------------------------------------------------------- Einstellungen: Praxis, Zuweisende, KI, Module
async def practice_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/praxis"
    if request.method == "POST":
        form = await request.post()
        fields = {k: _s(form, k, 500) for k in ("name", "street", "house_no", "zip", "city", "phone", "email", "gln", "zsr")}
        fields["address"] = ", ".join(x for x in (f"{fields['street']} {fields['house_no']}".strip(),
                                                  f"{fields['zip']} {fields['city']}".strip()) if x)
        fields["gln"], fields["zsr"] = _ids(fields["gln"], fields["zsr"], back)
        canton = _s(form, "canton", 2).upper()
        lang = _s(form, "default_lang", 2)
        if not fields["name"] or lang not in ("de", "fr") or not re.fullmatch(r"[A-Z]{2}", canton):
            raise redirect_with_flash(back, "settings.invalid")
        reply_time = _s(form, "reply_time", 20)
        core_practice.update(db, {**{k: (v or None) if k != "name" else v for k, v in fields.items()},
                                  "canton": canton, "default_lang": lang,
                                  "reply_time": reply_time if reply_time in core_practice.REPLY_TIMES else None})
        raise redirect_with_flash(back, "common.saved")
    return await settings_page(request, "praxis", "settings/s_practice.html", {"reply_times": core_practice.REPLY_TIMES})


async def insurers_page(request: web.Request) -> web.StreamResponse:
    """Versicherer: Name, Gesetz, GLN, Tiers garant/payant, Adresse (Pflicht für die elektronische Rechnung)."""
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/versicherer"
    billing_on = enabled(request, "billing")
    if request.method == "POST":
        form = await request.post()
        if form.get("do") == "toggle" and _id(form):
            core_patients.toggle_insurer(db, _id(form))
            raise redirect_with_flash(back, "common.saved")
        if form.get("do") == "add_md" and billing_on:          # aus dem MediData-Teilnehmerverzeichnis übernehmen
            from ..billing import api as billing_api

            from ...core.swissids import clean_gln

            row = billing_api.medidata_participant(db, clean_gln(_s(form, "gln", 40)) or _s(form, "gln", 13))
            law, tiers = _s(form, "law", 5), _s(form, "tiers", 10)
            if row is None or law not in ("KVG", "UVG", "IVG", "MVG", "VVG") or tiers not in ("garant", "payant"):
                raise redirect_with_flash(back, "settings.invalid")
            core_patients.save_insurer(db, {"name": row["name"][:120], "law": law, "tiers": tiers, "gln": row["gln_participant"],
                                            "street": (row["street"] or "")[:120] or None, "zip": (row["zip"] or "")[:9] or None,
                                            "city": (row["town"] or "")[:80] or None, "tp_group": None})
            raise redirect_with_flash(back, "common.saved")
        from ...core.swissids import clean_gln

        name, law, tiers, raw_gln = _s(form, "name", 120), _s(form, "law", 5), _s(form, "tiers", 10), _s(form, "gln", 40)
        gln = clean_gln(raw_gln) or ""          # «7601 0034 1234 5» wie auf der Versichertenkarte
        if not name or law not in ("KVG", "UVG", "IVG", "MVG", "VVG") or tiers not in ("garant", "payant") or (raw_gln and not gln):
            raise redirect_with_flash(back, "settings.invalid")
        group = _s(form, "tp_group", 20)
        if group and group not in core_patients.INSURER_TP_GROUPS:
            raise redirect_with_flash(back, "settings.invalid")
        core_patients.save_insurer(db, {"name": name, "law": law, "tiers": tiers, "gln": gln or None, "street": _s(form, "street", 120) or None,
                                        "zip": _s(form, "zip", 9) or None, "city": _s(form, "city", 80) or None, "tp_group": group or None},
                                   _id(form) if form.get("do") == "update" else None)
        raise redirect_with_flash(back, "common.saved")
    insurers = core_patients.all_insurers(db)
    md = {"reach": {}, "directory": None, "results": [], "q": ""}
    if billing_on:                                   # MediData: erreichbar? welche XML-Versionen? (Teilnehmerverzeichnis)
        from ..billing import api as billing_api

        md["directory"] = billing_api.medidata_directory(db)
        md["reach"] = billing_api.medidata_reach(db, [i["gln"] for i in insurers])
        md["q"] = (request.query.get("md") or "").strip()[:60]
        md["results"] = billing_api.medidata_search(db, md["q"]) if md["q"] else []
    return await settings_page(request, "versicherer", "settings/s_insurers.html", {"insurers": insurers, "md": md,
                                                                                     "tp_groups": core_patients.INSURER_TP_GROUPS})


async def referrers_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    pid = current_practice_id()
    back = f"{prefix(request)}/einstellungen/zuweisende"
    if request.method == "POST":
        form = await request.post()
        if form.get("do") == "toggle" and _id(form):
            core_patients.toggle_referrer(db, _id(form))
            raise redirect_with_flash(back, "common.saved")
        name = _s(form, "name", 120)
        lang = _s(form, "lang", 2)
        if not name or lang not in ("de", "fr", "it", "en"):
            raise redirect_with_flash(back, "settings.invalid")
        core_patients.save_referrer(db, {
            "name": name, "practice_name": _s(form, "practice_name", 200) or None, "specialty": _s(form, "specialty", 120) or None,
            "address": _s(form, "address", 500) or None, "phone": _s(form, "phone", 40) or None,
            "hin_email": _s(form, "hin_email", 200) or None, **dict(zip(("gln", "zsr"), _ids(_s(form, "gln", 40), _s(form, "zsr", 20), back))),
            "lang": lang,
        }, _id(form) if form.get("do") == "update" else None)
        raise redirect_with_flash(back, "common.saved")
    return await settings_page(request, "zuweisende", "settings/s_referrers.html", {
        "referrers": db.query("SELECT * FROM referrers WHERE practice_id = ? ORDER BY active DESC, name", (pid,))})


async def ai_page(request: web.Request) -> web.StreamResponse:
    """Nur einfache Zustandswörter (bereit / nicht eingerichtet) – Modelle, Adressen und Pfade bleiben bei NSystem."""
    # Hier wird wirklich nachgefragt (in einem Thread – der Rest der Praxis-Software wartet nicht mit)
    return await settings_page(request, "ki", "settings/s_ai.html", {"ai": await asyncio.to_thread(request.app[AI].status)})


async def modules_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    pid = current_practice_id()
    mods = request.app[MODULES]
    if request.method == "POST":
        form = await request.post()
        name = _s(form, "module", 40)
        if name in mods and mods[name].optional:
            on = form.get("enabled") == "1"
            set_module_enabled(db, pid, name, on)
            audit(db, "update", "practice_module", pid, None, {"module": name, "enabled": on})
            raise redirect_with_flash(f"{prefix(request)}/einstellungen/module", "common.saved")
        raise redirect_with_flash(f"{prefix(request)}/einstellungen/module", "settings.invalid")
    versions = {r["module"]: r["version"] for r in db.query("SELECT module, version FROM module_versions")}
    rows = []
    for m in mods.values():
        rows.append({"name": m.name, "title_key": m.title_key, "description_key": m.description_key, "optional": m.optional,
                     "enabled": module_enabled(db, pid, m.name, mods),
                     "version": versions.get(m.name, m.version), "requires": [mods[r].title_key for r in m.requires if r in mods],
                     "uses": [mods[u].title_key for u in m.uses if u in mods]})
    return await settings_page(request, "module", "settings/s_modules.html", {"mods": rows})


async def sms_page(request: web.Request) -> web.StreamResponse:
    """SMS-Anbieter wählen (ASPSMS oder eCall), Absendername, Test-SMS, letzte Sendungen."""
    db = request.app[DB]
    pid = current_practice_id()
    service = request.app[SMS]
    back = f"{prefix(request)}/einstellungen/sms"
    if request.method == "POST":
        form = await request.post()
        if form.get("do") == "mail_test":
            from ...core.mail import valid_email

            addr = _s(form, "email", 120)
            if not valid_email(addr):
                raise redirect_with_flash(back, "sms.mail_bad")
            t = request.app[I18N].t
            request.app[MAIL].send(pid, addr, t(request["lang"], "sms.mail_test_subject"), t(request["lang"], "sms.mail_test_text"))
            audit(db, "mail_test", "practice", pid)
            raise redirect_with_flash(back, "sms.test_queued")
        if form.get("do") == "test":
            number = core_sms.normalize_phone(_s(form, "phone", 30))
            if not number:
                raise redirect_with_flash(back, "sms.bad_number")
            service.send(pid, "sms", number, request.app[I18N].t(request["lang"], "sms.test_text"))
            audit(db, "sms_test", "practice", pid)
            raise redirect_with_flash(back, "sms.test_queued")
        provider = _s(form, "provider", 10)
        if provider not in service.available():
            raise redirect_with_flash(back, "settings.invalid")
        core_sms.set_provider(db, pid, provider, _s(form, "sender", 20) or None)
        audit(db, "update", "practice_sms", pid, None, {"provider": provider})
        raise redirect_with_flash(back, "common.saved")
    practice = core_practice.get(db)
    return await settings_page(request, "sms", "settings/s_sms.html", {
        "live": service.live, "available": service.available(), "chosen": practice["sms_provider"],
        "active": service.provider_of(pid), "sender": service.sender_of(pid), "sender_raw": practice["sms_sender"] or "",
        "items": core_sms.recent(db, pid), "mask": core_sms.mask,
        "mail_live": request.app[MAIL].live, "mail_from": request.app[SETTINGS].smtp_from})


async def hin_page(request: web.Request) -> web.StreamResponse:
    """HIN: Zustand des Anschlusses (aus der Box-Einstellung), Verbindungstest, letzte Sendungen."""
    import asyncio

    db = request.app[DB]
    pid = current_practice_id()
    settings = request.app[SETTINGS]
    mailbox = request.app[HIN]
    practice = core_practice.get(db)
    owns = mailbox.owns(practice["slug"])
    test = None
    if request.method == "POST" and owns:
        test = await asyncio.to_thread(_hin_test, mailbox)
        audit(db, "hin_test", "practice", pid, None, {"ok": test["ok"]})
    sent = db.query("SELECT recipient, body, status, error, created_at FROM outbox WHERE practice_id = ? AND channel = 'hin'"
                    " ORDER BY id DESC LIMIT 15", (pid,))
    return await settings_page(request, "hin", "settings/s_hin.html", {
        "owns": owns, "configured": mailbox.configured, "address": settings.hin_from if owns else "",
        "receive": owns and mailbox.can_receive, "smtp": f"{settings.hin_smtp_host}:{settings.hin_smtp_port}",
        "imap": f"{settings.hin_imap_host}:{settings.hin_imap_port}", "poll": settings.hin_poll_minutes,
        "sent": sent, "test": test,
        "missing_hin": db.value("SELECT COUNT(*) FROM referrers WHERE practice_id = ? AND active = 1 AND (hin_email IS NULL OR hin_email = '')",
                                (pid,), 0)})


def _hin_test(mailbox) -> dict:
    """Verbindung zum HIN Client prüfen (SMTP-Begrüssung, IMAP-Anmeldung). Es wird nichts versendet."""
    import smtplib

    out = {"smtp": False, "imap": None, "ok": False, "error": ""}
    try:
        server = smtplib.SMTP(mailbox.s.hin_smtp_host, mailbox.s.hin_smtp_port, timeout=10) if mailbox.s.hin_tls != "ssl" \
            else smtplib.SMTP_SSL(mailbox.s.hin_smtp_host, mailbox.s.hin_smtp_port, timeout=10)
        server.noop()
        server.quit()
        out["smtp"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"SMTP: {type(exc).__name__}"
    if mailbox.can_receive:
        try:
            conn = mailbox._imap()
            conn.logout()
            out["imap"] = True
        except Exception as exc:  # noqa: BLE001
            out["imap"] = False
            out["error"] = (out["error"] + " " if out["error"] else "") + f"IMAP: {type(exc).__name__}"
    out["ok"] = out["smtp"] and out["imap"] is not False
    return out


async def nsystem_page(request: web.Request) -> web.StreamResponse:
    """Was NSystem auf dieser Box getan hat (Befehle, Ergebnis, Protokoll) und ob die Box meldet. Keine Patientendaten gehen an NSystem.

    Supportzugang: nur hier (oder mit einer Support-Anfrage) öffnet die Leitung ihn – 1 bis 8 Stunden, Standard 4 – und sieht
    ihn mit Restzeit; früher schliessen jederzeit. Ausführen tut es der Runner (root), siehe core/nsystem.py."""
    from ...core import box, nsystem

    db = request.app[DB]
    settings = request.app[SETTINGS]
    pid = current_practice_id()
    back = f"{prefix(request)}/einstellungen/nsystem"
    nsystem.ingest_runner_events(db, settings)
    status = nsystem.support_status(settings)
    pending = nsystem.pending_session(db)
    if pending is not None and pending["requested_at"] < _ago(nsystem.LOCAL_MAX_AGE):
        pending = None                                   # Runner hat nicht reagiert → nicht mehr «wird geöffnet»
    if request.method == "POST":
        form = await request.post()
        do = form.get("do")
        if do == "support_open":
            hours = int(form.get("hours")) if str(form.get("hours") or "").isdigit() else 0
            if hours not in nsystem.SUPPORT_HOURS or not nsystem.support_configured(settings) or not nsystem.support_allowed(db, settings):
                raise redirect_with_flash(back, "settings.invalid")
            if status["open"] or pending is not None:
                raise redirect_with_flash(back, "nsystem.support.already")
            nsystem.open_support(db, settings, practice_id=pid, user_id=current_user_id(), hours=hours)
            audit(db, "support_access_open", "nsystem", None, None, {"hours": hours})
            raise redirect_with_flash(back, "nsystem.support.requested")
        if do == "support_close":
            nsystem.close_support(db, settings, practice_id=pid, user_id=current_user_id())
            audit(db, "support_access_close", "nsystem", None, None, None)
            raise redirect_with_flash(back, "nsystem.support.closing")
        raise redirect_with_flash(back, "settings.invalid")
    return await settings_page(request, "nsystem", "settings/s_nsystem.html", {
        "configured": box.configured(settings), "last": box.get_state(db, "last_report"),
        "error": box.get_state(db, "last_error"), "commands": box.commands(db),
        "labels": box.ACTIONS_FR if request["lang"] == "fr" else box.ACTIONS,
        "support": status, "support_configured": nsystem.support_configured(settings),
        "support_allowed": nsystem.support_allowed(db, settings), "pending": pending,
        "hours_options": nsystem.SUPPORT_HOURS, "default_hours": nsystem.SUPPORT_DEFAULT_HOURS,
        "events": nsystem.events(db, pid), "last_failed": _last_failed(db)})


def _ago(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def _last_failed(db):
    from ...core import nsystem

    rows = nsystem.sessions(db, 1)
    return rows[0] if rows and rows[0]["status"] == "failed" and rows[0]["requested_at"] >= _ago(3600) else None


def _section(key: str, label: str):
    return lambda request: {"key": key, "label_key": label, "path": key}


def setup(ctx: ModuleContext) -> None:
    from . import setup_check

    ctx.page("leitung", "/einrichtung", setup_check.page, ("GET", "POST"))
    ctx.slot("leitung.overview.card", 5, setup_check.card, "settings/card_setup.html")
    ctx.slot("leitung.overview.tile", 1, setup_check.tile)
    ctx.page("leitung", "/team", team_page, ("GET", "POST"))
    ctx.page("leitung", "/vertretungen", delegations_page, ("GET", "POST"))
    ctx.nav("leitung", NavItem("nav.team", "badge", "/team", order=40))
    ctx.nav("leitung", NavItem("nav.delegations", "swap_horiz", "/vertretungen", order=45))
    ctx.nav("leitung", NavItem("nav.settings", "tune", "/einstellungen", order=90))
    ctx.page("leitung", "/einstellungen", lambda request: _redirect(request))
    for key, order, handler in (("praxis", 10, practice_page), ("versicherer", 68, insurers_page), ("sms", 62, sms_page),
                                ("hin", 65, hin_page), ("zuweisende", 70, referrers_page),
                                ("ki", 80, ai_page), ("module", 90, modules_page), ("nsystem", 95, nsystem_page)):
        ctx.page("leitung", f"/einstellungen/{key}", handler, ("GET", "POST") if key != "ki" else ("GET",))
        ctx.slot("leitung.settings", order, _section(key, f"settings.sec.{key}"))


async def _redirect(request: web.Request) -> web.StreamResponse:
    raise web.HTTPFound(f"{prefix(request)}/einstellungen/praxis")
