"""NSystem-Anbindung der Praxis-Box.

- Die Box meldet alle 5 Minuten ihren Zustand an die NSystem-Übersicht (nur ausgehend, keine Patientendaten).
- In der Antwort können Befehle von NSystem stehen. Sie sind mit dem Schlüssel von NSystem signiert (Ed25519);
  die Box prüft Signatur, Box-ID, Ablaufzeit und ob der Befehl schon ausgeführt wurde.
- Ausgeführt wird nur, was auf der festen Liste steht (agent/actions.py) – durch einen eigenen Dienst mit Root-Rechten
  (n-fizyo-runner), der die Signatur ein zweites Mal prüft. Die Leitung der Praxis sieht jeden NSystem-Befehl.
- Passwörter setzt NSystem nie zurück: das macht die Leitung der Praxis bzw. die Person selbst.
"""
