"""Lokale KI. Alles läuft auf der Praxis-Box:

- Spracherkennung (STT): faster-whisper (optional), sonst Texteingabe.
- Notizentwurf und Berichtsauswertung: Ollama (lokales Sprachmodell) oder
  die eingebaute Regel-Engine (funktioniert ohne KI-Hardware).
- Übersetzung für Patientinnen und Patienten: Ollama (z. B. TranslateGemma).
- Texterkennung gescannter Berichte: Tesseract.

Ausnahme nur für Vorführungen mit fiktiven Daten: cloud.py (NFIZYO_AI_MODE=cloud_demo, nur mit NFIZYO_DEMO=1).

Grundsatz: Die KI schlägt vor, die Therapeutin / der Therapeut entscheidet.
"""
