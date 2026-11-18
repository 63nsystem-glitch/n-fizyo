"""HIN: gesicherte E-Mail im Gesundheitswesen (Health Info Net).

Die Box spricht mit dem HIN Client (läuft auf dem Praxis-Server und verschlüsselt selbst) oder mit einem HIN-Gateway:
  Senden:    SMTP  127.0.0.1:5018   (HIN Client, ohne Anmeldung – laut HIN-Anleitung)
  Empfangen: IMAP  127.0.0.1:5020   (HIN Client, Benutzer = HIN-Login)
Adresse, Ports und Zugangsdaten stehen NUR in der .env-Datei der Box (NFIZYO_HIN_*), nie in der Datenbank.
Das Postfach gehört genau einer Praxis (NFIZYO_HIN_PRACTICE = Kurzname). Eingehende Post landet nur dort.

Vor dem Echtbetrieb: HIN-Anschluss der Praxis (HIN-Identität) und HIN Client auf dem Server installieren.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from html.parser import HTMLParser

log = logging.getLogger(__name__)
MAX_ATTACHMENT = 25 * 1024 * 1024
IMAP_TIMEOUT = 60          # Sekunden je Netzwerkschritt: ein hängender HIN-Client darf das Abholen nie für immer blockieren


@dataclass
class Incoming:
    uid: str
    sender: str
    subject: str
    date: str
    text: str
    attachments: list[tuple[str, bytes]] = field(default_factory=list)
    too_large: list[tuple[str, int]] = field(default_factory=list)   # (Dateiname, Bytes) – nicht übernommen
    unreadable: bool = False                                          # Mail selbst nicht lesbar


class HinError(Exception):
    pass


class HinMailbox:
    def __init__(self, settings):
        self.s = settings

    @property
    def configured(self) -> bool:
        return bool(self.s.hin_from and self.s.hin_practice)

    @property
    def can_receive(self) -> bool:
        return self.configured and bool(self.s.hin_user)

    def owns(self, practice_slug: str | None) -> bool:
        return self.configured and practice_slug == self.s.hin_practice

    # ------------------------------------------------------------------ Senden
    def build(self, to: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]]) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.s.hin_from
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=self.s.hin_from.split("@")[-1] or "n-fizyo.local")
        msg.set_content(body)
        for filename, data, mime in attachments:
            main, _, sub = mime.partition("/")
            msg.add_attachment(data, maintype=main, subtype=sub or "octet-stream", filename=filename)
        return msg

    def send(self, to: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]]) -> str:
        """Blockierend (in einem Thread aufrufen). Gibt die Message-ID zurück."""
        if not self.configured:
            raise HinError("HIN ist in dieser Box nicht eingerichtet.")
        if "@" not in to:
            raise HinError("Keine gültige HIN-Adresse.")
        msg = self.build(to, subject, body, attachments)
        ctx = ssl.create_default_context()
        if self.s.hin_tls == "ssl":
            server = smtplib.SMTP_SSL(self.s.hin_smtp_host, self.s.hin_smtp_port, timeout=30, context=ctx)
        else:
            server = smtplib.SMTP(self.s.hin_smtp_host, self.s.hin_smtp_port, timeout=30)
        try:
            if self.s.hin_tls == "starttls":
                server.starttls(context=ctx)
            if self.s.hin_tls != "none" and self.s.hin_user and self.s.hin_password:
                server.login(self.s.hin_user, self.s.hin_password)  # HIN Client lokal: ohne Anmeldung
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
        return str(msg["Message-ID"])

    # ------------------------------------------------------------------ Empfangen
    def _imap(self):
        if self.s.hin_tls == "ssl":
            conn = imaplib.IMAP4_SSL(self.s.hin_imap_host, self.s.hin_imap_port, ssl_context=ssl.create_default_context(),
                                     timeout=IMAP_TIMEOUT)
        else:
            conn = imaplib.IMAP4(self.s.hin_imap_host, self.s.hin_imap_port, timeout=IMAP_TIMEOUT)
            if self.s.hin_tls == "starttls":
                conn.starttls(ssl_context=ssl.create_default_context())
        conn.login(self.s.hin_user, self.s.hin_password)
        return conn

    def fetch_unseen(self, limit: int = 20) -> list[Incoming]:
        """Ungelesene Nachrichten holen (blockierend). Sie werden erst mit mark_seen() als gelesen markiert."""
        if not self.can_receive:
            return []
        conn = self._imap()
        try:
            conn.select("INBOX")
            typ, data = conn.uid("search", None, "UNSEEN")
            if typ != "OK":
                return []
            out = []
            for uid in (data[0] or b"").split()[:limit]:
                typ, parts = conn.uid("fetch", uid, "(BODY.PEEK[])")
                if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                try:
                    out.append(parse(uid.decode(), parts[0][1]))
                except Exception:  # noqa: BLE001 - eine kaputte Mail darf das Postfach nicht blockieren
                    log.exception("HIN-Nachricht %s nicht lesbar", uid.decode())
                    out.append(Incoming(uid=uid.decode(), sender="", subject="", date="", text="", unreadable=True))
            return out
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    def mark_seen(self, uids: list[str]) -> None:
        if not uids:
            return
        conn = self._imap()
        try:
            conn.select("INBOX")
            for uid in uids:
                conn.uid("store", uid, "+FLAGS", "(\\Seen)")
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass


class _HtmlText(HTMLParser):
    """Nur der lesbare Text einer HTML-Mail (Absätze und Zeilen bleiben erhalten, Skripte/Styles fallen weg)."""
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _HtmlText()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001
        return ""
    lines = [" ".join(line.split()) for line in "".join(p.out).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _str(part) -> str:
    try:
        return part.get_content()
    except Exception:  # noqa: BLE001
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", "replace")


def parse(uid: str, raw: bytes) -> Incoming:
    """E-Mail zerlegen: Absender, Betreff, Text, Anhänge (nur Daten – nichts wird ausgeführt).
    Reine HTML-Mails (z. B. aus Spitalsystemen) liefern ihren Text; Bilder im Mailtext (Logo, Signatur) sind kein Anhang."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    text, html = "", ""
    attachments: list[tuple[str, bytes]] = []
    too_large: list[tuple[str, int]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if (filename and part.get("Content-ID") and part.get_content_disposition() != "attachment"
                and part.get_content_maintype() == "image"):
            continue   # eingebettetes Bild (image001.png in der Signatur) – gehört zum Mailtext
        if filename:
            payload = part.get_payload(decode=True) or b""
            if 0 < len(payload) <= MAX_ATTACHMENT:
                attachments.append((filename, payload))
            elif payload:
                too_large.append((filename, len(payload)))
        elif part.get_content_type() == "text/plain" and not text:
            text = _str(part)
        elif part.get_content_type() == "text/html" and not html:
            html = _str(part)
    if not text.strip() and html:
        text = html_to_text(html)
    return Incoming(uid=uid, sender=parseaddr(str(msg.get("From", "")))[1], subject=str(msg.get("Subject", ""))[:200],
                    date=str(msg.get("Date", "")), text=text[:20000], attachments=attachments, too_large=too_large)


def docx_text(data: bytes, limit: int = 20 * 1024 * 1024) -> str:
    """Text eines Word-Dokuments (.docx) – damit ein Bericht als Word-Datei nicht verloren geht. Leer, wenn nicht lesbar."""
    import io
    import zipfile
    from xml.etree import ElementTree

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if z.getinfo("word/document.xml").file_size > limit:
                return ""
            xml = z.read("word/document.xml")
        root = ElementTree.fromstring(xml)
    except Exception:  # noqa: BLE001 - kein gültiges docx
        return ""
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paras = []
    for p in root.iter(w + "p"):
        bits = []
        for el in p.iter():
            if el.tag == w + "t":
                bits.append(el.text or "")
            elif el.tag == w + "tab":
                bits.append("\t")
            elif el.tag in (w + "br", w + "cr"):
                bits.append("\n")
        paras.append("".join(bits))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(paras)).strip()


# --------------------------------------------------------------------------- Protokoll (Postausgang, Kern-Tabelle)
def record_sent(db, practice_id: int, to: str, what: str, *, ok: bool, ref: str | None = None, error: str | None = None) -> int:
    """HIN-Versand im Postausgang festhalten (nur Empfänger und «Bericht #…», kein Inhalt)."""
    from .db import utcnow

    now = utcnow()
    return db.execute(
        "INSERT INTO outbox (practice_id, channel, recipient, body, status, created_at, sent_at, provider, attempts, error, provider_ref)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (practice_id, "hin", to, what, "sent" if ok else "failed", now, now if ok else None, "hin", 1, (error or "")[:300] or None, ref),
    )
