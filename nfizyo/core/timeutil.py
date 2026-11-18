"""Zeit und Datum (Speicherung in UTC, Anzeige in Europe/Zurich)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Zurich")

WEEKDAYS = {
    "de": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"],
    "fr": ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."],
    "it": ["lun", "mar", "mer", "gio", "ven", "sab", "dom"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "pt": ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"],
    "es": ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"],
    "sq": ["Hën", "Mar", "Mër", "Enj", "Pre", "Sht", "Die"],
    "bks": ["pon", "uto", "sri", "čet", "pet", "sub", "ned"],
    "tr": ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"],
    "ar": ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"],
    "uk": ["пн", "вт", "ср", "чт", "пт", "сб", "нд"],
}
WEEKDAYS_LONG = {
    "de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
    "fr": ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
    "it": ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "pt": ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"],
    "es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
    "sq": ["e hënë", "e martë", "e mërkurë", "e enjte", "e premte", "e shtunë", "e diel"],
    "bks": ["ponedjeljak", "utorak", "srijeda", "četvrtak", "petak", "subota", "nedjelja"],
    "tr": ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"],
    "ar": ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"],
    "uk": ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"],
}
MONTHS = {
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"],
    "it": ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
    "pt": ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"],
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"],
    "sq": ["janar", "shkurt", "mars", "prill", "maj", "qershor", "korrik", "gusht", "shtator", "tetor", "nëntor", "dhjetor"],
    "tr": ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"],
    "ar": ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"],
    # Ukrainisch im Genitiv ("27 жовтня")
    "uk": ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"],
}


def now_local() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now_local().date()


def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def local_dt(d: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=TZ)


def day_bounds_utc(d: date) -> tuple[str, str]:
    start = datetime.combine(d, time(0, 0), tzinfo=TZ)
    return to_utc_iso(start), to_utc_iso(start + timedelta(days=1))


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def fmt_time(value: str | datetime | None) -> str:
    dt = from_iso(value) if isinstance(value, str) else value
    return dt.strftime("%H:%M") if dt else ""


def fmt_date(value: str | date | datetime | None, lang: str = "de", weekday: bool = False, year: bool = True) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            value = from_iso(value) if "T" in value else date.fromisoformat(value)
        except ValueError:
            return value[:32]          # alter, ungültiger Wert in der Datenbank: zeigen, wie er ist – die Seite bleibt benutzbar
    d = value.date() if isinstance(value, datetime) else value
    text = d.strftime("%d.%m.%Y" if year else "%d.%m.")
    if weekday:
        text = f"{WEEKDAYS.get(lang, WEEKDAYS['de'])[d.weekday()]} {text}"
    return text


def fmt_long_date(d: date, lang: str = "de") -> str:
    """Langes Datum ohne Jahr, z. B. "Dienstag, 27. Oktober" / "mardi 27 octobre"."""
    wd = WEEKDAYS_LONG.get(lang, WEEKDAYS_LONG["de"])[d.weekday()]
    if lang == "bks":  # Monatsnamen unterscheiden sich zwischen B/K/S – Zahl ist eindeutig
        return f"{wd}, {d.day}. {d.month}."
    month = MONTHS.get(lang, MONTHS["de"])[d.month - 1]
    patterns = {
        "fr": "{wd} {day} {month}",
        "it": "{wd} {day} {month}",
        "en": "{wd}, {day} {month}",
        "pt": "{wd}, {day} de {month}",
        "es": "{wd}, {day} de {month}",
        "sq": "{wd}, {day} {month}",
        "tr": "{day} {month} {wd}",
        "ar": "{wd}، {day} {month}",
        "uk": "{wd}, {day} {month}",
    }
    return patterns.get(lang, "{wd}, {day}. {month}").format(wd=wd, day=d.day, month=month)
