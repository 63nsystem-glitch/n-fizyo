"""N-Fizyo KI-Server (NSystem, Rechenzentrum in der Schweiz).

Für Praxen ohne eigene Grafikkarte: die Box in der Praxis schickt einzelne Aufgaben (Diktat-Audio, Berichtstext,
Satz zum Übersetzen) per HTTPS hierher und bekommt das Ergebnis zurück.

Grundsätze:
- Speichert keine Inhalte. Audio liegt nur während der Verarbeitung in einer temporären Datei und wird danach gelöscht.
- Protokolliert nur Metadaten (welche Praxis, welche Aufgabe, Dauer, Grösse) – für Kapazität und Abrechnung.
- Jede Praxis hat ein eigenes Token; gespeichert wird nur dessen Hash.
"""
