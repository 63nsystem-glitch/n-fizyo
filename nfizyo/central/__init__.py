"""NSystem-Übersicht (eigener Dienst auf einem Server von NSystem in der Schweiz).

- Empfängt die signierten Zustandsmeldungen der Praxis-Boxen (nur technische Angaben, keine Patientendaten).
- Zeigt alle Boxen, Warnungen (auch als Push aufs Telefon über ntfy) und eine Checkliste für neue Boxen.
- Stellt signierte Befehle aus der festen Liste aus (core/box.py); die Box holt sie mit ihrer nächsten Meldung ab.
- Anmeldung nur für NSystem-Personal, immer mit Zwei-Schritt-Anmeldung.
Die Box ist von aussen nicht erreichbar: alle Verbindungen gehen von der Box aus.
"""
