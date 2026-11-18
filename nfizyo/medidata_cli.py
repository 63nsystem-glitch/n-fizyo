"""Kommandozeile für den MediData-Webservice (für NSystem, nicht für die Praxis):

  medidata-csr        privaten Schlüssel (RSA 2048, Rechte 0600) und CSR erstellen (CN = X-Client-ID, C = CH, O, L)
  medidata-renew      CSR mit dem aktuellen Client-Zertifikat an MediData senden, neue Kette speichern (alte bleibt als .bak-…)
  medidata-client-id  neue X-Client-ID beim Wechsel von der MediData-Box / VA zum Webservice abfragen

Grundlage: MediData «Requesting a client certificate with CSR» V1.0 und «Requesting the new X-Client-ID» V1.1.
Ausgabe nur auf dem Bildschirm (nie ins Protokoll). Passwörter und Schlüssel werden nie angezeigt.
Ablauf und Befehle: docs/VERBINDUNGSTEST.md › MediData.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .modules.billing import medidata, medidata_cert as mc

ENV_HINT = "/etc/n-fizyo/n-fizyo.env"


def _say(out: list[str], text: str = "") -> None:
    out.append(text)


def csr(settings, *, org: str, city: str, client_id: str | None = None, key: str | None = None, csr_path: str | None = None,
        new_key: bool = False, out: list[str]) -> int:
    """Schlüssel (falls nötig) und CSR erstellen. Rückgabe: Exit-Code."""
    key_path = Path(key or settings.medidata_key) if (key or settings.medidata_key) else None
    if key_path is None:
        _say(out, "Fehler: Pfad des Schlüssels fehlt – NFIZYO_MEDIDATA_KEY in der .env oder --key angeben.")
        return 1
    cn = client_id or settings.medidata_client_id
    if not key_path.parent.is_dir():
        _say(out, f"Fehler: Ordner {key_path.parent} fehlt (z. B. sudo install -d -m 0700 -o nfizyo -g nfizyo {key_path.parent}).")
        return 1
    try:
        mc.validate_subject(client_id=cn or "", org=org, city=city)         # erst prüfen, dann Schlüssel erzeugen
        if new_key:
            target = mc.new_key_path(key_path)
            if target.exists():
                _say(out, f"Fehler: {target} gibt es schon (neuer Schlüssel noch nicht verwendet). Erst «medidata-renew» "
                          "ausführen oder die Datei bewusst löschen.")
                return 1
            private = mc.new_key()
            mc.write_private_key(target, private)
            _say(out, f"Neuer privater Schlüssel: {target} (RSA 2048, Rechte 0600)")
        elif key_path.exists():
            target = key_path
            private = mc.load_key(str(key_path))
            _say(out, f"Vorhandener Schlüssel wird verwendet: {key_path}")
        else:
            target = key_path
            private = mc.new_key()
            mc.write_private_key(target, private)
            _say(out, f"Privater Schlüssel erstellt: {target} (RSA 2048, Rechte 0600)")
        pem = mc.build_csr(private, client_id=cn or "", org=org, city=city)
    except mc.CertError as exc:
        _say(out, f"Fehler: {exc}")
        return 1
    path = Path(csr_path) if csr_path else mc.csr_path_for(target)
    try:
        mc.write_public_file(path, pem)
    except OSError as exc:
        _say(out, f"Fehler: CSR nicht gespeichert ({path}: {exc.strerror})")
        return 1
    _say(out, f"CSR erstellt: {path}")
    _say(out, f"  Inhalt: C=CH, L={city.strip()}, O={org.strip()}, CN=<X-Client-ID>")
    _say(out)
    _say(out, "Weiter:")
    _say(out, "  – Erstes Zertifikat: über das MediData-Kundenportal (laut MediData stellt auch das Portal Zertifikate aus;")
    _say(out, "    das genaue Vorgehen bei MediData erfragen). Kette als PEM speichern, in der .env: NFIZYO_MEDIDATA_CERT, _KEY.")
    _say(out, "  – Erneuern (ab 60 Tage vor Ablauf): python -m nfizyo medidata-renew")
    return 0


def _pick_csr(settings, csr_path: str | None) -> tuple[Path, Path | None]:
    """(CSR, neuer Schlüssel oder None). Ohne Angabe: zuerst client.new.csr (neuer Schlüssel), sonst client.csr."""
    key_path = Path(settings.medidata_key)
    nk = mc.new_key_path(key_path)
    if csr_path:
        path = Path(csr_path)
    elif mc.csr_path_for(nk).exists() and nk.exists():
        path = mc.csr_path_for(nk)
    else:
        path = mc.csr_path_for(key_path)
    return path, (nk if nk.exists() else None)


async def renew(settings, *, csr_path: str | None = None, accept: str = "pem", force: bool = False, out: list[str],
                now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    if not (settings.medidata_cert and settings.medidata_key):
        _say(out, "Fehler: NFIZYO_MEDIDATA_CERT und NFIZYO_MEDIDATA_KEY fehlen – die Erneuerung braucht das aktuelle Zertifikat.")
        return 1
    st = mc.status(settings, now)
    if st.state == "error":
        _say(out, f"Fehler: aktuelles Zertifikat nicht brauchbar – {st.message}")
        return 1
    if st.state == "expired":
        _say(out, "Fehler: Das aktuelle Zertifikat ist abgelaufen. Ein neues gibt es nur noch über das MediData-Kundenportal.")
        return 1
    if st.days_left is not None and st.days_left > mc.RENEWAL_DAYS and not force:
        _say(out, f"Noch nicht fällig: gültig bis {st.valid_to:%d.%m.%Y} ({st.days_left} Tage). MediData erlaubt die Erneuerung "
                  f"ab {st.renew_from:%d.%m.%Y} (60 Tage vor Ablauf). Nichts gesendet.")
        _say(out, "Mit --force trotzdem senden (gleicher CSR: MediData liefert das schon ausgestellte Zertifikat).")
        return 0
    try:
        path, new_key = _pick_csr(settings, csr_path)
        request = mc.load_csr(path)
        cn = mc.common_name(request.subject)
        if settings.medidata_client_id and cn != settings.medidata_client_id:
            _say(out, "Fehler: CN im CSR ist nicht NFIZYO_MEDIDATA_CLIENT_ID. CSR mit «medidata-csr» neu erstellen.")
            return 1
        want = mc.spki(request.public_key())
        if new_key is not None and mc.spki(mc.load_key(str(new_key)).public_key()) == want:
            use_new = new_key
        elif mc.spki(mc.load_key(settings.medidata_key).public_key()) == want:
            use_new = None
        else:
            _say(out, f"Fehler: {path} gehört zu keinem vorhandenen Schlüssel. CSR mit «medidata-csr» neu erstellen.")
            return 1
        _say(out, f"Sende {path} an {mc.endpoint(settings)} …")
        issued = await mc.request_certificate(settings, request.public_bytes(_pem()), accept=accept)
        done = mc.install(settings, issued, request, new_key=use_new, now=now)
    except mc.CertError as exc:
        _say(out, f"Fehler: {exc}")
        return 1
    except mc.RenewError as exc:
        _say(out, f"Fehler: {exc}")
        return 1
    leaf = done["leaf"]
    _say(out, f"Neues Zertifikat gespeichert: {settings.medidata_cert}")
    _say(out, f"  gültig {leaf.not_valid_before_utc:%d.%m.%Y} – {leaf.not_valid_after_utc:%d.%m.%Y}, Seriennummer {leaf.serial_number:X}")
    if use_new is not None:
        _say(out, f"  neuer Schlüssel übernommen: {settings.medidata_key}")
        new_csr = mc.csr_path_for(use_new)
        if path == new_csr and new_csr.exists():                             # CSR passt jetzt zum eingetragenen Schlüssel
            new_csr.replace(mc.csr_path_for(Path(settings.medidata_key)))
    for what, backup in done["backups"].items():
        _say(out, f"  Sicherung ({'Zertifikat' if what == 'cert' else 'Schlüssel'}): {backup}")
    _say(out, "Kein Neustart nötig (N-Fizyo liest das Zertifikat bei jeder Sendung). Prüfen: python -m nfizyo check-connections")
    _say(out, f"Hinweis: höchstens {mc.MAX_ACTIVE} aktive Zertifikate pro Client – das alte bleibt bis zu seinem Ablauf aktiv.")
    return 0


def _pem():
    from cryptography.hazmat.primitives.serialization import Encoding

    return Encoding.PEM


async def client_id(settings, *, instance_id: str, env_file: str | None, out: list[str]) -> int:
    try:
        new_id = await medidata.new_client_id(settings, instance_id)
    except medidata.MigrationError as exc:
        _say(out, f"Fehler: {exc}")
        return 1
    env = env_file or ENV_HINT
    _say(out, f"Neue X-Client-ID: {new_id}")
    _say(out)
    _say(out, "So weiter (nur in der .env-Datei eintragen – nicht in Chat, E-Mail oder Tickets):")
    _say(out, f"  1. {env}:  NFIZYO_MEDIDATA_CLIENT_ID={new_id}")
    _say(out, "  2. Client-Zertifikat mit dieser ID als CN: python -m nfizyo medidata-csr --org … --city …")
    _say(out, "  3. Adresse des Webservice eintragen (aus dem Kundenportal, z. B. NFIZYO_MEDIDATA_URL=https://services.medidata.ch/md/srv/id0),")
    _say(out, "     dann: sudo systemctl restart n-fizyo")
    _say(out, "  4. python -m nfizyo check-connections")
    return 0
