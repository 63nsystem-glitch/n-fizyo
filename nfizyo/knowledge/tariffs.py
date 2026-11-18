"""Tarifkatalog Physiotherapie Schweiz – nur Positionen mit geprüfter Quelle.

Stand 7. November 2026:
- KVG: Tarifstruktur 312 (vom Bundesrat festgelegt, Stand 2018) gilt weiter. Taxpunktwert: kantonal bzw.
  vertraglich – die Praxis trägt ihn selbst ein. Eine neue KVG-Struktur (Einzelleistungen in 5-Minuten-Einheiten,
  Positionen 27xxx) wurde am 2. April 2026 dem Bundesrat zur Genehmigung eingereicht, geplante Einführung
  1. Januar 2027 – noch NICHT genehmigt, kein amtlicher Katalog, deshalb hier noch nicht aktiv.
  Quellen: BAG, Änderung der Verordnung über die Tarifstrukturen (Physiotherapie) – «Genehmigungsgesuch» vom 2.4.2026,
  keine Auskunft vor Abschluss; physioswiss «Die eingereichte KVG-Tarifstruktur im Überblick» (Stand 12.10.2026):
  Genehmigungsverfahren beim BAG läuft, Einführung «frühestens per 1. Januar 2027», «definitive Zeitpunkt» offen.
  Was bei Genehmigung einzutragen ist (Daten + wenige Regeln): docs/FATURA.md › «Yeni KVG tarif yapısı».
- UV/MV/IV: neuer Physiotherapietarif ab 1. Juli 2025, Abrechnung pro 5 Minuten, Taxpunktwert CHF 1.00 national.
  Die alten Ziffern 73xx gelten für UV/MV/IV nicht mehr. Quelle: physioswiss Merkblatt MTK-Tarif (13.3.2025).
  Externer Faktor (Korrektur der Tarifhöhe um 6 %): 0,94 ab 1. Oktober 2026. Quelle: MTK/ZMT, Physiotherapie ambulant
  (mtk-ctm.ch, «Der externe Faktor beträgt somit 0,94 und gilt ab dem 1. Oktober 2026»).
- Tarifcode in der elektronischen Rechnung (XML 5.0, Attribut tariff_type): 312 = KVG, 311 = UV/MV/IV
  (MTK-CTM: «Tarifcode 311», Physiotherapie ambulant).

KVG-Positionen 312 vollständig nach «Tarifstruktur für physiotherapeutische Leistungen, gültig ab 1. Januar 2018»
(Tarifcode 312, physioswiss/BAG), inkl. Kombinationsregeln (KVG_ALLOWED) – geprüft November 2026.
Neue KVG-Struktur: sobald der Bundesrat sie genehmigt und der amtliche Katalog vorliegt, als eigener Tarif mit valid_from
eintragen (z. B. «kvg2027», valid_from 2027-01-01) und bei «kvg312» valid_until setzen. Die Abrechnung wählt den Tarif nach
Behandlungstag und trennt Rechnungen an der Grenze automatisch (tariff_for_law(law, on=…)).
Französische Bezeichnungen der UV/MV/IV-Positionen: Übersetzung N-Fizyo (offizielle Bezeichnung prüfen).
"""
from __future__ import annotations

from dataclasses import dataclass

SOURCE_KVG = "Tarifstruktur für physiotherapeutische Leistungen (Tarifcode 312), gültig ab 1.1.2018"
SOURCE_UV = "physioswiss, Merkblatt Physiotherapietarif UV/MV/IV, gültig ab 01.07.2025"


@dataclass(frozen=True)
class Tariff:
    key: str
    laws: tuple[str, ...]
    name_de: str
    name_fr: str
    valid_from: str
    tp_value: float | None  # None = Praxis trägt Taxpunktwert ein
    source: str
    code: str = ""          # tariff_type in der XML-Rechnung
    external_factors: tuple[tuple[str, float], ...] = ()  # (gültig ab, Faktor), aufsteigend
    valid_until: str | None = None                          # letzter Behandlungstag (None = offen)
    default_code: str | None = None   # Zeittarif: Standardposition pro 5 Min. (sonst Ziffer der Behandlungsart)
    remote_code: str | None = None    # Position für Behandlung auf räumliche Distanz (Video), falls der Tarif eine hat
    # Zeittarif: Behandlungsart (Ziffer der KVG-Sitzungspauschale, z. B. 7340 MTT) → eigene Position dieses Tarifs.
    # Ohne Eintrag gilt default_code. So bleibt die Behandlungsart einmal eingestellt, auch wenn der Tarif wechselt.
    session_map: tuple[tuple[str, str], ...] = ()
    marker_code: str | None = None    # Kennzeichnungs-Position je Behandlung ohne Taxpunkte (neue KVG-Struktur: 27100)

    def code_for(self, session_code: str | None, video: bool = False) -> str | None:
        if video and self.remote_code:
            return self.remote_code
        return dict(self.session_map).get(session_code or "", self.default_code)

    def external_factor(self, on: str) -> float:
        """Externer Faktor am Behandlungstag (Standard 1.0)."""
        value = 1.0
        for since, factor in self.external_factors:
            if on >= since:
                value = factor
        return value


@dataclass(frozen=True)
class Position:
    tariff: str
    code: str
    label_de: str
    label_fr: str
    tp: float
    unit: str  # session | 5min | flat | km | min | chf (fester Frankenbetrag) | chf_var (Betrag je Rechnung, z. B. Material)
    note: str = ""
    group: str = "session"  # session (Sitzungspauschale) | supplement (Zuschlag / Pauschale)
    max_minutes: int | None = None   # Limitation: höchstens so viele Minuten je Behandlung (5-Minuten-Positionen)
    divided: bool = False            # Divisormethode: Zeit der Sitzung wird auf die anwesenden Patient/innen verteilt (Gruppe)

    @property
    def fixed_chf(self) -> bool:
        """Frankenbetrag statt Taxpunkte: kein Taxpunktwert, kein externer Faktor."""
        return self.unit in ("chf", "chf_var")


TARIFFS = {
    "kvg312": Tariff("kvg312", ("KVG",), "Physiotherapie KVG (Tarifstruktur 312)", "Physiothérapie LAMal (structure tarifaire 312)",
                     "2018-01-01", None, SOURCE_KVG, code="312"),
    "uvmviv": Tariff("uvmviv", ("UVG", "MVG", "IVG"), "Physiotherapietarif UV/MV/IV", "Tarif de physiothérapie AA/AM/AI",
                     "2025-07-01", 1.00, SOURCE_UV, code="311", external_factors=(("2025-07-01", 1.0), ("2026-10-01", 0.94)),
                     default_code="25.110", remote_code="25.310",
                     session_map=(("7313", "25.120"), ("7330", "25.410"), ("7340", "25.520"))),
}

POSITIONS = (
    # KVG, Tarifstruktur 312 (gültig ab 1.1.2018) – Sitzungspauschalen
    Position("kvg312", "7301", "Allgemeine Physiotherapie", "Physiothérapie générale", 48, "session"),
    Position("kvg312", "7311", "Aufwändige Physiotherapie", "Physiothérapie complexe", 77, "session"),
    Position("kvg312", "7313", "Hippotherapie", "Hippothérapie", 77, "session"),
    Position("kvg312", "7330", "Gruppentherapie (2 bis 5 Patient/innen)", "Thérapie de groupe (2 à 5 patient·e·s)", 25, "session"),
    Position("kvg312", "7340", "Medizinische Trainingstherapie (MTT)", "Thérapie par l'entraînement médical (MTT)", 22, "session",
             note="Für die Instruktion zum MTT-Programm statt 7340 insgesamt höchstens zwei Sitzungen nach 7301."),
    # KVG 312 – Zuschläge und Pauschalen
    Position("kvg312", "7350", "Zuschlag erste Behandlung", "Supplément première séance", 24, "flat", group="supplement",
             note="Pro Fall nur zur ersten Sitzung: einmal in 36 Sitzungen, bei Rezidiv mit verändertem Krankheitsbild oder nach > 6 Monaten."),
    Position("kvg312", "7351", "Zuschlag Kinder mit chronischer Behinderung (bis 6. Lebensjahr)",
             "Supplément enfants avec handicap chronique (jusqu'à 6 ans)", 30, "flat", group="supplement",
             note="Nur mit 7311, ärztlich begründet."),
    Position("kvg312", "7352", "Zuschlag Geh- oder Schwimmbad", "Supplément bassin de marche ou piscine", 19, "flat", group="supplement"),
    Position("kvg312", "7353", "Zuschlag Infrastruktur Hippotherapie", "Supplément infrastructure hippothérapie", 67, "flat",
             group="supplement"),
    Position("kvg312", "7354", "Zuschlag Weg- oder Zeitentschädigung (Domizilbehandlung)", "Supplément déplacement (traitement à domicile)",
             34, "flat", group="supplement", note="Nur bei ärztlich verordneter Domiziltherapie; nicht in Spital, Klinik, Heim."),
    Position("kvg312", "7361", "Behandlungsmaterial (Einkaufspreis)", "Matériel de traitement (prix d'achat)", 0, "chf_var",
             group="supplement", note="Kategorie, Menge mit Einheit und Einkaufspreis angeben."),
    Position("kvg312", "7362", "Pauschale Vaginalsonde", "Forfait sonde vaginale", 50, "chf", group="supplement",
             note="Einmal pro Kalenderjahr, nicht mit 7363 kumulierbar."),
    Position("kvg312", "7363", "Pauschale Analsonde", "Forfait sonde anale", 90, "chf", group="supplement",
             note="Einmal pro Kalenderjahr, nicht mit 7362 kumulierbar."),
    Position("uvmviv", "25.110", "Patientenbehandlung (Einzelsetting), pro 5 Min.", "Traitement du patient (individuel), par 5 min", 10.03, "5min"),
    Position("uvmviv", "25.120", "Hippotherapie (Einzelsetting), pro 5 Min.", "Hippothérapie (individuel), par 5 min", 18.26, "5min"),
    Position("uvmviv", "25.130", "Wohnungs- oder Arbeitsplatzabklärung, pro 5 Min.", "Évaluation du domicile ou du poste de travail, par 5 min", 10.03, "5min"),
    Position("uvmviv", "25.210", "Robotik obere Extremität, pro 5 Min.", "Robotique membre supérieur, par 5 min", 11.57, "5min"),
    Position("uvmviv", "25.220", "Robotik untere Extremität, pro 5 Min.", "Robotique membre inférieur, par 5 min", 14.44, "5min"),
    Position("uvmviv", "25.310", "Patientenbehandlung auf räumliche Distanz, pro 5 Min.", "Traitement à distance, par 5 min", 10.03, "5min"),
    # Gruppe: Divisormethode, höchstens 75 Min. je Behandlungseinheit (physioswiss «Gut zu wissen 16: Gruppentherapie», 20.11.2025)
    Position("uvmviv", "25.410", "Patientenbehandlung (Gruppensetting), pro 5 Min.", "Traitement en groupe, par 5 min", 10.38, "5min",
             max_minutes=75, divided=True),
    Position("uvmviv", "25.510", "MTT-Einführung / Reevaluation, pro 5 Min.", "Introduction / réévaluation MTT, par 5 min", 10.23, "5min"),
    Position("uvmviv", "25.520", "MTT-Training (selbständig)", "Entraînement MTT (autonome)", 48.47, "session"),
    Position("uvmviv", "25.610", "Zuschlag zweite/r Physiotherapeut/in, Einzelsetting, pro 5 Min.", "Supplément 2e physiothérapeute, individuel, par 5 min", 9.28, "5min"),
    Position("uvmviv", "25.630", "Zuschlag Sonn- und Feiertage, pro 5 Min.", "Supplément dimanches et jours fériés, par 5 min", 3.65, "5min"),
    Position("uvmviv", "25.640", "Zuschlag Geh-/Schwimmbad, Einzelsetting", "Supplément bassin, individuel", 30.00, "flat"),
    Position("uvmviv", "25.710", "Auswertung/Koordination ohne Patient/in, pro 5 Min.", "Évaluation/coordination sans patient·e, par 5 min", 9.72, "5min"),
    Position("uvmviv", "25.720", "Besprechung/Beratung, pro 5 Min.", "Entretien/conseil, par 5 min", 9.72, "5min"),
    Position("uvmviv", "25.730", "Bericht 1 (formalisiert)", "Rapport 1 (formalisé)", 26.20, "flat"),
    Position("uvmviv", "25.740", "Bericht 2", "Rapport 2", 52.40, "flat"),
    Position("uvmviv", "25.750", "Bericht 3", "Rapport 3", 104.80, "flat"),
    Position("uvmviv", "25.770", "Wegentschädigung, pro Min.", "Indemnité de déplacement, par min", 2.32, "min"),
)

BY_CODE = {(p.tariff, p.code): p for p in POSITIONS}
MAX_SESSIONS_PER_PRESCRIPTION = 9  # KVG und UV/MV/IV: max. 9 Behandlungen pro Verordnung

# KVG 312: Zuschlag → erlaubte Sitzungspauschalen derselben Sitzung (Tarifstruktur, Spalte «Kombinationen»).
# 7340 (MTT) ist mit keinem Zuschlag kombinierbar.
KVG_ALLOWED = {
    "7350": {"7301", "7311", "7313"},
    "7351": {"7311"},
    "7352": {"7301", "7311", "7330"},
    "7353": {"7313"},
    "7354": {"7301", "7311"},
    "7361": {"7301", "7311", "7330"},
    "7362": {"7301", "7311"},
    "7363": {"7301", "7311"},
}


def tariff_for_law(law: str | None, on: str | None = None) -> Tariff | None:
    """Amtlicher Tarif für das Gesetz – am Behandlungstag `on` (ISO-Datum). Ohne Datum: der jüngste gültige."""
    found = [t for t in TARIFFS.values() if law in t.laws and (on is None or (t.valid_from <= on and (t.valid_until is None
                                                                                                       or on <= t.valid_until)))]
    if on is None:
        found = [t for t in found if t.valid_until is None] or found
    return max(found, key=lambda t: t.valid_from) if found else None


def positions(tariff: str) -> list[Position]:
    return [p for p in POSITIONS if p.tariff == tariff]


def label(p: Position, lang: str) -> str:
    return p.label_fr if lang == "fr" else p.label_de
