"""0.9.0 – Texte, Übersetzungen, Formate: keine rohen Übersetzungsschlüssel, keine Technik für Anwender/innen,
Schweizer Formate (Geld, Telefon, Datum), Einzahl/Mehrzahl, Namen, Fehlerseiten in allen Patientensprachen, ?lang=."""
from __future__ import annotations

import html as html_lib
import re
from collections import deque

import pytest

from conftest import login, pid_of
from nfizyo.core.keys import I18N

# --------------------------------------------------------------------------- Rohe Schlüssel auf allen Seiten des Personals
_SKIP = re.compile(r"(abmelden|logout|\.pdf|\.csv|\.xlsx|\.zip|\.ics|\.xml|/download|/datei|/export/|/audio|/foto|/bild|/qr|/drucken|/druck)", re.I)
_TOKEN = re.compile(r"(?<![\w@./:-])([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)(?![\w@/-])")
# 127.0.0.1 selbst kommt im Test als Adresse der Seite vor (public_url); fest eingebaute Ports und Pfade nicht
_TECH = re.compile(r"NFIZYO_|\.env\b|docs/[A-Z]|deploy/|localhost|:8080|:11434|large-v3|tesseract|/var/lib/", re.I)


def _visible(page: str) -> str:
    page = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    attrs = " ".join(m.group(2) for m in re.finditer(r'\s(title|aria-label|placeholder|alt|data-confirm)="([^"]*)"', page))
    text = re.sub(r"<[^>]+>", " ", page)
    return html_lib.unescape(text + " " + attrs)


def _raw_keys(text: str, catalog: dict, namespaces: set[str]) -> set[str]:
    found = set()
    for tok in _TOKEN.findall(text):
        tok = tok.rstrip(".")
        if "." not in tok:
            continue
        if tok in catalog or (tok.split(".")[0] in namespaces and tok.count(".") >= 2):
            found.add(tok)
    return found


async def _crawl(client, start: str, prefix: str, limit: int = 90) -> dict[str, str]:
    seen, pages, queue = {start}, {}, deque([start])
    while queue and len(pages) < limit:
        url = queue.popleft()
        r = await client.get(url, allow_redirects=True)
        if r.status != 200 or "text/html" not in r.headers.get("Content-Type", ""):
            continue
        page = await r.text()
        pages[url] = page
        for href in re.findall(r'href="(/[^"#]*)"', page):
            href = html_lib.unescape(href)
            if href.startswith(prefix) and href not in seen and not _SKIP.search(href) and href.count("?") <= 1:
                seen.add(href)
                queue.append(href)
    return pages


@pytest.mark.parametrize("email,start,prefix", [
    ("therapie@demo.n-fizyo.ch", "/therapie/heute", "/therapie"),
    ("empfang@demo.n-fizyo.ch", "/empfang/heute", "/empfang"),
    ("leitung@demo.n-fizyo.ch", "/leitung/uebersicht", "/leitung"),
])
@pytest.mark.parametrize("ui_lang", ["de", "fr"])
async def test_no_raw_keys_or_technical_text_on_staff_pages(client, app, email, start, prefix, ui_lang):
    from nfizyo.core.keys import DB

    app[DB].execute("UPDATE users SET lang = ? WHERE email = ?", (ui_lang, email))
    await login(client, email)
    catalog = app[I18N].catalog[ui_lang]
    namespaces = {k.split(".")[0] for k in app[I18N].catalog["de"]}
    pages = await _crawl(client, start, prefix)
    assert len(pages) > 25, list(pages)
    problems = {}
    for url, page in pages.items():
        text = _visible(page)
        bad = _raw_keys(text, catalog, namespaces)
        tech = set(_TECH.findall(text))
        if bad or tech:
            problems[url] = sorted(bad | tech)
    assert not problems, problems


def test_raw_key_detector_itself():
    catalog = {"video.title": "Video", "settings.needs": "braucht:"}
    ns = {"video", "settings", "p"}
    text = "Module settings.mod_desc.rxwatch · video.title · info@nsystem.ch · demo.n-fizyo.ch · z. B. 2.5 kg · Datei bericht.pdf"
    assert _raw_keys(text, catalog, ns) == {"settings.mod_desc.rxwatch", "video.title"}


# --------------------------------------------------------------------------- Fehlerseiten der Patient/innen
@pytest.mark.parametrize("lang", ["de", "fr", "it", "en", "pt", "es", "sq", "bks", "tr", "ar", "uk"])
async def test_patient_error_pages_in_every_language(client, app, lang):
    t = app[I18N].t
    client.session.cookie_jar.update_cookies({"nf_lang": lang})
    r = await client.get("/p/gibt-es-nicht", headers={"Accept": "text/html"})
    page = await r.text()
    assert r.status == 404
    assert t(lang, "p.error.title_404") in page and t(lang, "p.error.home") in page
    assert 'href="/p/anmelden"' in page and 'href="/"' not in page.split("</head>")[1]
    assert f'lang="{lang}' in page
    if lang != "de":
        assert t("de", "p.error.title_404") not in page


def patient_cookie(client, app, first_name: str) -> None:
    from nfizyo.core.auth import create_session
    from nfizyo.core.keys import DB

    token, _ = create_session(app[DB], "patient", patient_id=pid_of(app, first_name), practice_id=1)
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({"nf_p": token})


async def test_patient_error_page_links_to_app_home_when_signed_in(client, app):
    patient_cookie(client, app, "Lea")
    page = await (await client.get("/p/gibt-es-nicht", headers={"Accept": "text/html"})).text()
    assert 'href="/p/heute"' in page and 'href="/p/anmelden"' not in page


async def test_staff_error_page_stays_german_or_french(client):
    await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get("/therapie/gibt-es-nicht", headers={"Accept": "text/html"})).text()
    assert "Nicht gefunden" in page and 'href="/"' in page


# --------------------------------------------------------------------------- Körperkarte in Patientenformularen
@pytest.mark.parametrize("lang", ["it", "en", "pt", "es", "sq", "bks", "tr", "ar", "uk"])
async def test_patient_body_map_is_translated_and_has_no_skeleton(client, app, lang):
    from nfizyo.core.keys import DB

    app[DB].execute("UPDATE patients SET lang = ? WHERE id = ?", (lang, pid_of(app, "Lea")))
    patient_cookie(client, app, "Lea")
    t = app[I18N].t
    for url in ("/p/anliegen", "/p/melden"):
        r = await client.get(url)
        page = await r.text()
        assert r.status == 200, url
        assert t(lang, "p.body.front") in page and t(lang, "p.body.back") in page, url
        for german in (">Vorne<", ">Hinten<", "Skelett", "data-bf-layer", "data-bone="):
            assert german not in page, (url, german)


# --------------------------------------------------------------------------- ?lang= auf öffentlichen Seiten, Online-Termine
async def test_public_pages_honour_lang_parameter(client, app):
    t = app[I18N].t
    r = await client.get("/p/anmelden?lang=tr")
    page = await r.text()
    assert 'lang="tr' in page and t("tr", "p.login.title") in page
    assert client.session.cookie_jar.filter_cookies(client.make_url("/p/")).get("nf_lang").value == "tr"
    page = await (await client.get("/p/anmelden")).text()                       # gemerkt
    assert 'lang="tr' in page
    page = await (await client.get("/p/anmelden?lang=xx")).text()               # ungültig: bleibt
    assert 'lang="tr' in page
    page = await (await client.get("/login?lang=fr")).text()                    # Personal: nur DE/FR
    assert 'lang="fr' in page
    page = await (await client.get("/login?lang=tr")).text()
    assert 'lang="fr' in page


async def test_booking_practice_texts_machine_translated_with_original(client, app):
    from nfizyo.ai.translate import store_translation
    from nfizyo.core.keys import DB

    db = app[DB]
    slug = db.value("SELECT slug FROM practices WHERE id = 1")
    ty = db.one("SELECT t.id, t.name_de FROM booking_types b JOIN treatment_types t ON t.id = b.treatment_type_id"
                " WHERE b.practice_id = 1 ORDER BY t.id LIMIT 1")
    db.execute("UPDATE booking_settings SET intro_de = ? WHERE practice_id = 1", ("Bitte 10 Minuten früher kommen.",))
    store_translation(db, 1, ty["name_de"], "de", "ar", "علاج طبيعي", "test")
    store_translation(db, 1, "Bitte 10 Minuten früher kommen.", "de", "ar", "يرجى الحضور قبل 10 دقائق.", "test")
    page = await (await client.get(f"/p/termin/{slug}?lang=ar")).text()
    assert 'dir="rtl"' in page
    assert '<bdi dir="auto">علاج طبيعي</bdi>' in page and "يرجى الحضور قبل 10 دقائق." in page
    assert app[I18N].t("ar", "p.machine_translated") in page and ty["name_de"] in page   # Original abrufbar
    assert f'/p/datenschutz?praxis={slug}' in page
    page = await (await client.get(f"/p/termin/{slug}?lang=de")).text()               # Deutsch: Original, kein Hinweis
    assert app[I18N].t("de", "p.machine_translated") not in page


async def test_privacy_names_practice_when_coming_from_booking(client, app):
    from nfizyo.core.keys import DB

    name = app[DB].value("SELECT name FROM practices WHERE id = 1")
    slug = app[DB].value("SELECT slug FROM practices WHERE id = 1")
    assert name not in await (await client.get("/p/datenschutz")).text()
    assert name in await (await client.get(f"/p/datenschutz?praxis={slug}")).text()
    referer = str(client.make_url(f"/p/termin/{slug}"))
    assert name in await (await client.get("/p/datenschutz", headers={"Referer": referer})).text()
