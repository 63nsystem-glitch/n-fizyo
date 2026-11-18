"""Oberfläche 0.9.0: Telefon-Leiste mit «Mehr», genau ein aktiver Menüpunkt, Demo-Hinweis ohne Überlagerung,
Agenda auf dem Telefon, Vorlese-Knopf nur mit Stimme, Speichern unter dem Hauptformular, leere Einschränkungen mit Aktion."""
from __future__ import annotations

import re

from conftest import csrf_of, login, pid_of

from nfizyo.core.keys import DB


def _between(html: str, start: str, end: str) -> str:
    i = html.index(start)
    return html[i:html.index(end, i)]


async def test_phone_bar_has_four_areas_and_more_sheet(client):
    await login(client, "therapie@demo.n-fizyo.ch")
    html = await (await client.get("/therapie/heute")).text()
    bar = _between(html, '<nav class="bottom-nav"', "</nav>")
    assert len(re.findall(r"<a href=", bar)) == 4 and "data-nav-more" in bar and 'data-dialog="dlg-more"' in bar
    # kurze Beschriftung in der Leiste, lange in der Seitenleiste
    assert ">Patienten<" in bar and "Meine Patienten" not in bar
    sheet = _between(html, '<dialog class="sheet-bottom" id="dlg-more"', "</dialog>")
    for href in ("/therapie/agenda", "/therapie/uebungen", "/therapie/anatomie", "/therapie/nachrichten", "/therapie/konto"):
        assert f'href="{href}"' in sheet, href
    # Konto-Symbol oben im Kopf (nicht in einer eigenen Zeile unter dem Titel)
    assert "topbar-account" in _between(html, '<header class="topbar">', "</header>")


async def test_more_tab_marks_pages_inside_the_sheet(client):
    await login(client, "therapie@demo.n-fizyo.ch")
    html = await (await client.get("/therapie/anatomie")).text()
    assert 'class="nav-more on"' in html
    html = await (await client.get("/therapie/heute")).text()
    assert 'class="nav-more"' in html


async def test_screen_switch_reachable_on_phone_with_own_icon(client):
    await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get("/leitung/uebersicht")).text()
    sheet = _between(html, 'id="dlg-more"', "</dialog>")
    assert 'href="/start"' in sheet and 'href="/leitung/vertretungen"' in sheet


async def test_only_the_most_specific_menu_item_is_active(client):
    await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get("/empfang/agenda/warteliste")).text()
    rail = _between(html, '<div class="rail-items">', "</div>")
    active = re.findall(r'<a class="nav" href="([^"]+)" aria-current="page"', rail)
    assert active == ["/empfang/agenda/warteliste"]
    html = await (await client.get("/empfang/agenda")).text()
    rail = _between(html, '<div class="rail-items">', "</div>")
    assert re.findall(r'<a class="nav" href="([^"]+)" aria-current="page"', rail) == ["/empfang/agenda"]


async def test_demo_hint_sits_in_the_page_not_over_it(client):
    html = await (await client.get("/login")).text()
    assert "demo-ribbon" not in html and 'class="demo-tag"' in _between(html, '<div class="brand">', "</div>")
    await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get("/empfang/heute")).text()
    assert "demo-ribbon" not in html and html.index('class="demo-strip"') < html.index('<header class="topbar">')


async def test_agenda_phone_day_view_hint_and_legend(client):
    await login(client, "therapie@demo.n-fizyo.ch")
    html = await (await client.get("/therapie/agenda")).text()
    # Therapie öffnet die Woche; ohne ausdrückliche Wahl schaltet das Telefon (agenda.js) auf den Tag
    assert "data-agenda-auto-day" in html and "agenda.js" in html
    assert 'class="agenda-legend"' in html and "Arbeitszeit" in html and "data-swipe-hint" in html
    html = await (await client.get("/therapie/agenda?view=woche")).text()
    assert "data-agenda-auto-day" not in html
    html = await (await client.get("/therapie/agenda?view=tag")).text()
    assert "data-agenda-auto-day" not in html


async def test_read_aloud_button_starts_hidden(client, app):
    """Ohne Stimme auf dem Gerät bleibt der Knopf unsichtbar (speak.js zeigt ihn nur mit Stimme) – kein deaktivierter Knopf."""
    from test_homeprogram_package import first_item, lea

    await lea(client)
    html = await (await client.get(f"/p/uebungen/{first_item(app, 'Lea')}")).text()
    buttons = re.findall(r"<button[^>]*data-speak=[^>]*>", html)
    assert buttons and all(" hidden" in b and "disabled" not in b for b in buttons)


async def test_settings_save_button_under_main_form(client):
    await login(client, "leitung@demo.n-fizyo.ch")
    for path in ("praxis", "zahlung", "online-termine"):
        html = await (await client.get(f"/leitung/einstellungen/{path}")).text()
        form = _between(html, '<form method="post" class="grid-main-side">', "</form>")
        side_start = form.index("grid-side")
        assert form.count('type="submit"') == 1 and 'class="form-actions form-foot"' in form, path
        assert form.index("form-foot") > side_start, path
        # Reiter umbrechen, der aktive ist markiert
        tabs = _between(html, '<nav class="tabs tabs-wrap"', "</nav>")
        assert f'/einstellungen/{path}" aria-current="page"' in tabs


async def test_empty_restrictions_offer_an_add_action(client, app):
    await login(client, "therapie@demo.n-fizyo.ch")
    pid = pid_of(app, "Peter")      # Demo: ohne Einschränkungen
    assert not app[DB].value("SELECT COUNT(*) FROM findings WHERE patient_id = ? AND kind IN ('restriction', 'clearance', 'red_flag')", (pid,))
    html = await (await client.get(f"/therapie/patienten/{pid}")).text()
    card = _between(html, 'aria-labelledby="h-restr"', "</section>")
    assert "card-empty" in html and 'data-dialog="dlg-finding"' in card and '{"kind": "restriction"}' in card


async def test_working_hours_grouped_per_person(client):
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get("/leitung/einstellungen/zeiten")).text()
    assert html.count('class="card hours-week"') >= 2 and 'class="hours-grid"' in html
    assert csrf_of(html) == csrf
