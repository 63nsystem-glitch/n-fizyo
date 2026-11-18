# Auftragsbearbeitungsvertrag (AVV) – N-Fizyo

> **ENTWURF – vor Verwendung durch eine Juristin / einen Juristen (Datenschutz, Berufsgeheimnis) prüfen lassen.**
> Grundlage: Art. 9 DSG (nDSG), Art. 321 StGB (Berufsgeheimnis Physiotherapie), Datenschutzverordnung (DSV). Stand 12. November 2026 (Version 0.9.0).

## 1. Parteien

- **Verantwortliche:** [Name der Praxis], [Adresse] – nachfolgend «Praxis».
- **Auftragsbearbeiterin:** NSystem GmbH, [Adresse], Biel/Bienne – nachfolgend «NSystem».

## 2. Gegenstand

NSystem stellt der Praxis die Software N-Fizyo bereit und betreibt, wartet und aktualisiert sie (Installation in der Praxis
oder auf einem Server in der Schweiz). Dabei kann NSystem im Ausnahmefall mit Personendaten der Praxis in Berührung kommen
(z. B. Fehleranalyse auf Anfrage der Praxis). NSystem bearbeitet Personendaten **nur auf dokumentierte Weisung der Praxis**.

## 3. Art der Daten und betroffene Personen

- Patientinnen und Patienten: Personalien, Kontakt, Versicherung, Verordnungen, Berichte, Behandlungsdokumentation,
  Messwerte, Eingaben in der Patienten-App, Termine, Rechnungen, Einwilligungen – **besonders schützenswerte Gesundheitsdaten**.
- Mitarbeitende der Praxis: Name, Rolle, Anmeldedaten (Passwörter nur als Hash), Arbeitszeiten.
- Zuweisende Ärztinnen und Ärzte, Versicherer: Name, Adresse, GLN, HIN-Adresse.

## 4. Pflichten von NSystem

1. **Berufsgeheimnis:** NSystem und alle beigezogenen Personen sind Hilfspersonen im Sinne von Art. 321 StGB und wahren
   das Berufsgeheimnis; die Pflicht besteht über das Vertragsende hinaus. Mitarbeitende werden schriftlich verpflichtet.
2. **Zweckbindung:** keine Bearbeitung für eigene Zwecke, keine Weitergabe, keine Auswertung für Werbung oder Training von KI-Modellen.
3. **Ort:** Speicherung und Bearbeitung **ausschliesslich in der Schweiz**. Keine Bekanntgabe ins Ausland.
4. **Datensicherheit (Art. 8 DSG, Art. 1–6 DSV):** technische und organisatorische Massnahmen gemäss Anhang A.
5. **Kein Zugriff im Normalbetrieb:** Die NSystem-Übersicht erhält von der Praxis-Installation nur technische Zustandsdaten
   (Version, Speicher, Stand der Sicherung, Zustand der Dienste – keine Patientendaten). NSystem kann keine Passwörter
   zurücksetzen und meldet sich nie mit einem Konto der Praxis an. Jede Aktion von NSystem an der Installation ist für die
   Leitung der Praxis sichtbar (Protokoll).
5a. **Fernzugriff im Support (Weisung der Praxis):** nur wenn die Leitung ihn in der Software freigibt, für 1 bis 8 Stunden;
   über einen verschlüsselten Tunnel; nur eine feste Liste von Wartungsbefehlen (keine freie Kommandozeile); jede Aktion im
   Protokoll; der Zugang schliesst sich zur gesetzten Zeit selbst. Die Freigabe gilt als dokumentierte Weisung im Sinne von Ziffer 2.
5b. **Support-Anfragen:** Texte mit erkennbaren Patientendaten werden in der Software schon beim Schreiben blockiert; NSystem
   speichert solche Texte nicht.
6. **Meldung von Verletzungen der Datensicherheit:** unverzüglich, spätestens innerhalb von 24 Stunden nach Kenntnis, mit allen
   Angaben, die die Praxis für ihre Meldung an den EDÖB (Art. 24 DSG) und an Betroffene braucht.
7. **Unterstützung:** bei Auskunfts-, Berichtigungs- und Löschbegehren (Art. 25 ff. DSG) und bei einer
   Datenschutz-Folgenabschätzung (Art. 22 DSG).
8. **Unterauftragsbearbeiter:** nur mit vorgängiger schriftlicher Zustimmung der Praxis und gleichwertigen Pflichten (Anhang B).

## 5. Pflichten der Praxis

- Rechtmässigkeit der Bearbeitung, Information der Patientinnen und Patienten (Datenschutzerklärung in der App: `/p/datenschutz`),
  Einwilligungen (App, E-Mail, eigene Videos, Video-Sitzungen), Aufbewahrungsfristen nach kantonalem Recht.
- Verwaltung der Zugänge (Rollen, Zwei-Schritt-Anmeldung, Austritte sofort sperren).
- Schlüssel und Passwörter (SMS, HIN, MediData, Video) nur in der `.env` des Servers – nie per E-Mail oder Chat.

## 6. Kontrolle

Die Praxis kann die Einhaltung prüfen (Auskunft, Unterlagen, nach Absprache Prüfung vor Ort durch eine zur Verschwiegenheit
verpflichtete Fachperson).

## 7. Dauer und Ende

Der Vertrag gilt so lange, wie NSystem für die Praxis Leistungen erbringt. Bei Vertragsende: vollständiger Datenexport
(Funktion «Export › vollständiger Datenexport», ZIP) an die Praxis und danach Löschung aller Daten bei NSystem und
Unterauftragsbearbeitern innert 30 Tagen, mit schriftlicher Bestätigung. Gesetzliche Aufbewahrungspflichten der Praxis bleiben
bei der Praxis.

## 8. Haftung, Gerichtsstand

[Nach Hauptvertrag.] Anwendbar ist Schweizer Recht. Gerichtsstand: [Biel/Bienne].

---

## Anhang A – Technische und organisatorische Massnahmen (wie in N-Fizyo umgesetzt)

| Bereich | Massnahme |
|---|---|
| Zugang | Persönliche Konten, Rollen (Therapie, Empfang, Leitung), Zwei-Schritt-Anmeldung (Authenticator-App) für alle Praxiskonten |
| Zugriff | Therapeut/innen sehen nur Patient/innen ihres Behandlungsteams (inkl. Vertretung); Empfang ohne Diagnosen |
| Trennung | Strikte Trennung der Praxen in der Datenbank; automatisierte Tests, die nie gelöscht werden dürfen |
| Protokoll | Änderungen an Akten, Freigaben, Einwilligungen, Video-Räumen, Angeboten u. a. im Audit-Protokoll |
| Übermittlung | HTTPS; Berichte an Ärzt/innen über HIN; E-Mail an Patient/innen nur mit Einwilligung |
| Patienten-App | Anmeldung mit Telefonnummer, Geburtsdatum und SMS-Code; Links (Video, Terminangebote) nur als Hash gespeichert, zeitlich begrenzt |
| Video | Eigener Jitsi-Server in der Schweiz, Zutritt nur mit Schlüssel pro Termin, keine Aufzeichnung, keine Drittanbieter |
| KI | Lokal auf dem Server der Praxis oder KI-Server von NSystem in der Schweiz, der nichts speichert; KI schlägt nur vor, Fachperson entscheidet |
| Sicherung | Tägliche Sicherung (nur root lesbar, Integritätsprüfung), Prüfung beim Update, automatische Rückkehr bei Fehlern (deploy/update.sh); optional verschlüsselte Kopie ausser Haus (AES-256, Verschlüsselung auf dem Server der Praxis, Schlüssel nur dort und auf Papier bei der Praxis – der Speicheranbieter sieht keine Inhalte) |
| Löschung | Eigene Übungsvideos durch Patient/in jederzeit löschbar; Ton-Aufnahmen nach Freigabe gelöscht |
| Updates | Neue Versionen digital signiert (Ed25519), Signatur wird vor der Installation geprüft; ältere Versionen werden abgelehnt; Sicherung vor jedem Update, automatische Rückkehr bei Fehlern |
| Anmeldung | Passwörter nur als Hash (scrypt); Abmeldung nach Inaktivität; Begrenzung von Anmeldeversuchen und SMS-Codes |
| Patienten-App | Die Praxis kann die App einer Person auf allen Geräten abmelden (z. B. Telefon verloren); Benachrichtigungen per SMS/E-Mail ohne Inhalt |
| Aufbewahrung technischer Daten | Erledigte Hintergrundaufgaben 30 Tage (fehlgeschlagene 90), Versandprotokoll SMS/E-Mail und Benachrichtigungen 90 Tage, Kopie eingescannter Dokumente im Scan-Ordner 14 Tage |
| Fernzugriff | Nur mit Freigabe der Leitung, zeitlich begrenzt, feste Wartungsbefehle, Protokoll (Ziffer 4.5a) |
| KI im Demo-Modus | Ein externer KI-Dienst ist nur für Vorführungen mit fiktiven Daten zugelassen; sobald eine echte Praxis im System ist, sperrt die Software ihn technisch |

## Anhang B – Unterauftragsbearbeiter (von der Praxis auszufüllen / zu bestätigen)

| Leistung | Anbieter | Ort |
|---|---|---|
| Server (falls nicht in der Praxis) | [z. B. Infomaniak / Exoscale] | Schweiz |
| SMS | [ASPSMS (VADIAN.NET AG) oder eCall] – Vertrag direkt mit der Praxis | Schweiz |
| Elektronische Rechnung | MediData AG – Vertrag direkt mit der Praxis | Schweiz |
| Gesicherte E-Mail | HIN – Vertrag direkt mit der Praxis | Schweiz |
| KI-Server (nur Betriebsart «KI-Server») | NSystem GmbH | Schweiz |
| Externe Sicherung, verschlüsselt (falls eingerichtet) | [Anbieter des Speichers – Entscheid der Praxis] | Schweiz |
