# Medizinprodukt-Software? – Fragen für die Abklärung (MepV / EU-MDR)

> **Keine rechtliche Beurteilung.** Diese Liste dient als Grundlage für eine Fachperson (Regulatory Affairs).
> In der Schweiz gilt die Medizinprodukteverordnung (MepV), angelehnt an die EU-MDR (u. a. Anhang VIII, Regel 11;
> Leitfaden MDCG 2019-11). Massgebend ist die **Zweckbestimmung**, die der Hersteller festlegt.

## Funktionen und vorgesehene Zweckbestimmung

| Funktion | Was sie tut | Entscheidet sie? | Frage an die Fachperson |
|---|---|---|---|
| Verwaltung (Agenda, Abrechnung, Kasse, Verordnungen im Blick) | Organisation, Fristen, Tarifregeln | nein | voraussichtlich kein Medizinprodukt – bestätigen |
| Diktat → Notizentwurf (KI) | Text aus Sprache, Struktur | nein, Fachperson prüft und gibt frei | Dokumentationshilfe ohne Diagnosezweck – kein MP? |
| Übersetzung, Vorlesen | Sprache | nein | kein MP? |
| Kurzüberblick, Übergabe | zeigt vorhandene Daten zusammen | nein | Anzeige ohne Bewertung – kein MP? |
| Heimprogramm, eigenes Video, «nicht geübt» | Anleitung der Fachperson, Rückmeldung der Patient/in | nein | Kommunikationsmittel – kein MP? |
| **MTT: Vorschläge zur Steigerung** | berechnet aus Wiederholungen, Anstrengung, Schmerz einen Gewichtsvorschlag | **nein – Übernahme nur durch Fachperson** | Ist eine regelbasierte Therapie-Empfehlung «Software, die Informationen liefert, die zu Entscheidungen zu therapeutischen Zwecken herangezogen werden» (Regel 11)? Genügt die Zweckbestimmung «Erinnerungshilfe, Entscheidung bei der Fachperson»? |
| **Schmerz-Warnung (MTT, Heimprogramm)** | hebt Schmerz ≥ 5 hervor | nein | reine Anzeige eines Schwellenwerts – MP-relevant? |

## Was N-Fizyo heute schon einhält (unabhängig vom Ergebnis)

- KI und Regeln schlagen nur vor; jede Änderung am Plan bzw. jeder Text an Patient/innen braucht die Freigabe der Fachperson.
- Keine medizinischen Ratschläge in der Patienten-App; auf jeder Seite «Im Notfall rufen Sie 144 an».
- Regeln sind offen dokumentiert (`nfizyo/modules/mtt/rules.py`) und getestet.

## Mögliche Ergebnisse

1. Kein Medizinprodukt → Zweckbestimmung schriftlich festhalten, Marketing-Aussagen danach ausrichten.
2. Medizinprodukt (z. B. Klasse I/IIa) → Funktion «Vorschläge zur Steigerung» abschaltbar machen (Modul-Einstellung) und
   entweder Konformitätsverfahren oder Funktion für die Schweiz deaktivieren.
