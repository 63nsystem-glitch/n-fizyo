"""Alle Module von N-Fizyo. Reihenfolge = Reihenfolge der Datenbank-Migrationen.

Ein neues Modul: Ordner anlegen (siehe core/modules.py für den Aufbau), Ausweis (MODULE) ausfüllen und hier eintragen.
Mehr nicht – Bildschirme, Menüs und Akte holen sich die Beiträge selbst.
"""
from __future__ import annotations

import importlib

from ..core.modules import Module

MODULE_ORDER = (
    "patients",      # Akte (Therapie) und Stammblatt (Empfang): Körperkarte, Befunde, Messwerte
    "anatomy",       # Anatomie-Atlas und Strukturblatt
    "agenda",        # Heute, Kalender, Standorte, Räume, Geräte, Serien, Warteliste, Erinnerungen
    "documentation", # Verlaufsnotizen mit Versionen und Sperre
    "dictation",     # Diktat → Transkript → Notizentwurf (Telefon, offline)
    "patientinfo",   # Information für Patient/innen aus dem Diktat (übersetzt, vorlesbar)
    "inbox",         # Posteingang: Berichte, Scan-Ordner, Texterkennung, KI-Vorschläge
    "reports",       # Bericht an Zuweisende
    "rxwatch",       # Verordnungen im Blick: freie Sitzungen, 5-Wochen-Frist, 36 Sitzungen, Anfragen an Ärzt/innen
    "homeprogram",   # Übungsbibliothek und Heimprogramme
    "messages",      # Nachrichten und Problem-Meldungen der Patient/innen
    "intake",        # Anliegen in der eigenen Sprache (Patienten-App)
    "briefing",      # Kurzüberblick vor dem Termin und Übergabe beim Wechsel der Therapeutin / des Therapeuten
    "patientapp",    # Patienten-App (QR-Einladung, Anmeldung, Startseite, Termine)
    "notify",        # Benachrichtigungen an Patient/innen (E-Mail mit Einverständnis oder SMS, gebündelt, keine SMS in der Nacht)
    "booking",       # Online-Termine: öffentliche Anfrage mit SMS-Code, Bestätigung durch den Empfang
    "billing",       # Abrechnung (Tarif KVG 312 und UV/MV/IV, Preisliste Selbstzahler), QR-Rechnung, XML 5.0, Zahlungen, Mahnungen
    "cashdesk",      # Kasse: Produkte, Abos (MTT), Selbstzahler – bar/Twint/Karte/Rechnung, Quittung, Tagesabschluss
    "mtt",           # MTT-Fläche: Geräte mit QR-Code, Trainingsplan, Sätze aus der App, Live-Ansicht, Steigerungs-Vorschläge
    "firms",         # Firmenkunden: Rechnungen an Firmen (Preisliste oder frei), QR-Zahlteil, Zahlungen aus der Bankdatei, Mahnungen
    "video",         # Video-Sitzung über den eigenen Jitsi-Server der Praxis (Schweiz): Raum pro Termin, Link per SMS, keine Aufzeichnung
    "dataimport",    # Datenübernahme aus der bisherigen Software (Excel/CSV)
    "exports",       # Export: Buchhaltung (Journal CSV, Excel) und vollständiger Datenexport (ZIP)
    "stats",         # Übersicht der Leitung (Kennzahlen aus den Modulen)
    "support",       # Hilfe: Support-Anfrage an NSystem (ohne Patientendaten), Antwort in der App
    "settings",      # Einstellungen der Leitung: Praxis, Team, Vertretungen, KI, Module
)


def load_modules() -> list[Module]:
    modules = []
    for name in MODULE_ORDER:
        pkg = importlib.import_module(f"{__name__}.{name}")
        module: Module = pkg.MODULE
        if module.name != name:
            raise RuntimeError(f"Modulname {module.name!r} passt nicht zum Ordner {name!r}")
        modules.append(module)
    return modules
