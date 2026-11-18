"""Demo-Daten der Fachmodule (Berichte, Notizen, Heimprogramm, Nachrichten, Anliegen …). Alles fiktiv."""
from __future__ import annotations

from datetime import timedelta

from ..core.keys import SETTINGS
from ..core.timeutil import fmt_date
from .seed import EXTRA_STEPS, Demo

MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def step(fn):
    EXTRA_STEPS.append(fn)
    return fn


def _long_de(d) -> str:
    return f"{d.day}. {MONTHS_DE[d.month - 1]} {d.year}"


def _long_fr(d) -> str:
    return f"{d.day} {MONTHS_FR[d.month - 1]} {d.year}"


# --------------------------------------------------------------------------- Berichte (Posteingang)
OP_REPORT = """Spital Beispiel, Orthopädie (fiktive Klinik – Demo)
Dr. med. A. Beispielarzt, Facharzt Orthopädische Chirurgie

Biel, {doc}

Operationsbericht und Austritt

Betrifft: Frau Lea Muster, geb. 05.03.1992

Diagnosen:
- Ruptur des vorderen Kreuzbandes rechts nach Unihockey-Unfall am {accident}
- Status nach VKB-Rekonstruktion rechts mit Semitendinosus-Sehne am {op}

Verlauf:
Komplikationsloser postoperativer Verlauf, reizlose Wundverhältnisse. Kein Infekt.

Procedere:
Teilbelastung mit 15 kg an Gehstöcken für 2 Wochen, danach Vollbelastung nach Schmerz.
Keine Knieextension in offener Kette zwischen 90° und 45° für 12 Wochen.
Keine Kräftigung der Hamstrings gegen Widerstand für 6 Wochen.
Physiotherapie: 9 Sitzungen.

Versicherung: Suva, Unfall (UVG)
"""

CONTROL_REPORT = """Dr. med. P. Beispiel, Orthopädie Seeland (fiktiv – Demo)

Biel, {doc}

Kontrollbericht

Betrifft: Frau Lea Muster, geb. 05.03.1992

Diagnose: Status nach VKB-Rekonstruktion rechts mit Semitendinosus-Sehne am {op}.

Befund: Klinische Kontrolle 5 Wochen postoperativ. Erguss rückläufig, Lachman-Test fest. Flexion aktiv ca. 100°.

Procedere: Freigabe Velo-Ergometer ohne Widerstand ab sofort. Weiterhin keine Knieextension in offener Kette zwischen 90° und 45° bis 12 Wochen postoperativ. Nächste Kontrolle in 6 Wochen.

Versicherung: Suva, Unfall (UVG)
"""

REFERRAL_HANS = """Hausarztpraxis Altstadt (fiktiv – Demo)
Dr. med. C. Muster

Datum: {doc_num}

Zuweisung Physiotherapie

Patient: Herr Hans Beispiel, geb. 12.03.1961

Diagnose: Lumboischialgie rechts bei Diskushernie L4/5 (MRI vom {mri_num})

Befund: Schmerzen lumbal mit Ausstrahlung ins rechte Bein bis zum Fuss, Lasègue rechts positiv bei 50°.
Keine Blasen- oder Mastdarmstörung. Keine Parese. Nachtschmerz verneint.

Verordnung: 9 Behandlungen Physiotherapie, Rumpfstabilisation, Instruktion Heimprogramm.
Bitte Rückmeldung bei Verschlechterung oder neuen neurologischen Ausfällen.

Versicherung: Helsana (KVG)
"""

REPORT_YVES = """Centre Médical du Jura (clinique fictive – démonstration)
Dr B. Exempledoc, chirurgie orthopédique

Bienne, le {doc}

Concerne : Monsieur Yves Exemple, né le 11.09.1968

Diagnostic :
- Rupture partielle du tendon du supra-épineux de l'épaule droite
- Conflit sous-acromial droit
- État après réinsertion arthroscopique de la coiffe des rotateurs le {op_num}

Évolution :
Évolution favorable, pas d'infection. Légère raideur de l'épaule droite. Paresthésies intermittentes de la main droite à surveiller.

Suite de traitement :
Attelle d'abduction pendant 6 semaines.
Pas d'élévation active au-delà de 90° pendant 6 semaines.
Mobilisation passive autorisée dès maintenant.
Physiothérapie : 9 séances.

Assurance : Suva, accident (LAA), n° de sinistre 12.34567.26.1
"""


@step
def documents(app, demo: Demo) -> None:
    from ..ai.extract import extract_rules
    from ..core import patients as core_patients
    from ..modules.inbox import repo as irepo

    db = demo.db
    base = app[SETTINGS].documents_dir
    op, accident = demo.d(-35), demo.d(-74)

    def add(name, text, *, done=False, kind="report"):
        did, _ = irepo.store_file(db, base, name, text.encode("utf-8"), source="demo", uploaded_by=demo.users["rec"])
        result = extract_rules(text, today=demo.today)
        matches = core_patients.find_by_name_birthdate(db, result.patient_name, result.patient_birthdate)
        irepo.save_extraction(db, did, text, "text", result, matches)
        if done:
            db.execute("UPDATE suggestions SET status = 'accepted' WHERE document_id = ? AND status IN ('open','duplicate')", (did,))
            db.execute("UPDATE documents SET status = 'done', done_at = created_at WHERE id = ?", (did,))
        return did

    op_doc = add("OP-Bericht_Muster_Lea.txt", OP_REPORT.format(doc=_long_de(op + timedelta(days=3)), accident=fmt_date(accident),
                                                                op=fmt_date(op)), done=True)
    db.execute("UPDATE findings SET source_document_id = ? WHERE patient_id = ? AND source = 'document'", (op_doc, demo.patients["lea"]))
    add("Kontrollbericht_Muster.txt", CONTROL_REPORT.format(doc=_long_de(demo.d(-2)), op=fmt_date(op)))
    add("Zuweisung_Beispiel_Hans.txt", REFERRAL_HANS.format(doc_num=fmt_date(demo.d(-6)), mri_num=fmt_date(demo.d(-12))))
    add("Rapport_Exemple_Yves.txt", REPORT_YVES.format(doc=_long_fr(demo.d(-8)), op_num=fmt_date(demo.d(-27))))
    # Ein Scan ohne eindeutige Zuordnung – für den Empfang (Posteingang › Zuzuordnen)
    add("Scan_Verordnung.txt", SCAN_UNCLEAR.format(doc=_long_de(demo.d(-1))))


SCAN_UNCLEAR = """Hausarztpraxis Altstadt (fiktiv)
Dr. med. C. Muster

Biel, {doc}

Verordnung Physiotherapie

Patientin: A. Oernek
Diagnose: Lumbalgie, Verlaufskontrolle
9 Sitzungen Physiotherapie, Kontrolle nach 6 Sitzungen.
"""


# --------------------------------------------------------------------------- Notizen (Verlauf)
@step
def notes(app, demo: Demo) -> None:
    from ..core.tenant import practice_scope
    from ..modules.documentation import repo as nrepo

    db = demo.db
    p, u = demo.patients, demo.users
    lea = [
        (-28, "Erstbefund 1 Woche postoperativ. Flexion aktiv 60°, Extension -10°. Erguss deutlich, VAS 5/10. Gang an zwei Stöcken mit Teilbelastung.",
         "Lymphdrainage, Patellamobilisation, Quadrizeps-Aktivierung, Instruktion Heimprogramm.",
         "Typischer früher postoperativer Verlauf.", "Belastungsaufbau gemäss OP-Bericht, Heimprogramm täglich."),
        (-21, "Flexion 75°, Extension -5°. Erguss rückläufig. VAS 4/10.",
         "Mobilisation, Fersenrutschen, Gangschule.", "Gute Fortschritte.", "Stöcke ab nächster Woche reduzieren."),
        (-14, "Flexion 90°, Extension 0°. Gang ohne Stöcke im Haus. VAS 3/10.",
         "Mini-Kniebeugen 0–45°, Gleichgewicht, Velo noch nicht.", "Verlauf gut, Quadrizeps noch schwach.",
         "Kontrolle beim Operateur abwarten (Velo-Freigabe)."),
    ]
    for days, b, t, e, pl in lea:
        with practice_scope(demo.practice_id, u["td"]):
            nid = nrepo.create(db, p["lea"], {"befund": b, "behandlung": t, "beurteilung": e, "plan": pl},
                               source="dictation", session_date=demo.iso(days), author_id=u["td"])
            nrepo.approve(db, nid)

    def appt_day(key: str) -> str:
        from ..core.timeutil import from_iso

        return from_iso(db.value("SELECT starts_at FROM appointments WHERE id = ?", (demo.appointments[key],))).date().isoformat()

    # Notizen, die an Termine gebunden sind (Datum = Termin): Lea letzte Woche, Maria, Luca bei L. Esempio
    linked = (
        ("lea", "lea_past3", "td", {
            "befund": "Flexion 100°, Extension 0°. Kaum Erguss. VAS 3/10 nach Belastung. Einbeinstand rechts 20 s.",
            "behandlung": "Kniebeugen 0–60°, Step-ups, Gleichgewicht auf der Matte.",
            "beurteilung": "Gute Fortschritte, Quadrizeps-Kraft rechts noch deutlich vermindert.",
            "plan": "Kraft weiter aufbauen, Velo-Ergometer sobald Freigabe vom Operateur. Zu Hause Einbeinstand üben."}),
        ("maria", "maria_past1", "td", {
            "befund": "Abduktion links 95°, Aussenrotation schmerzhaft ab 30°. Nachtschmerz VAS 6/10.",
            "behandlung": "Mobilisation glenohumeral, Skapula-Kontrolle, Aussenrotation mit Band.",
            "beurteilung": "Beweglichkeit besser, Nachtschmerz unverändert.",
            "plan": "Schlafposition besprechen (Kissen unter dem Arm). Aussenrotation mit Band steigern."}),
        ("luca", "luca_past0", "le", {
            "befund": "OSG links: Dorsalextension 10°, Schwellung lateral gering. Einbeinstand links 15 s (rechts 45 s). VAS 3/10 bei Belastung.",
            "behandlung": "MTT: Wadenheben beidbeinig, Beinpresse 40 kg 3×12, Balance-Pad, Koordinationsleiter.",
            "beurteilung": "Propriozeption links noch deutlich vermindert.",
            "plan": "Steigern: Wadenheben einbeinig, Sprünge beidbeinig vorbereiten."}),
        ("luca", "luca_past1", "le", {
            "befund": "Dorsalextension 15°. Einbeinstand links 30 s. Hüpfen beidbeinig schmerzfrei. VAS 2/10.",
            "behandlung": "MTT: Beinpresse 50 kg 3×12, Wadenheben einbeinig 3×15, kurzes Seilspringen.",
            "beurteilung": "Gute Fortschritte. Joggen erst nach Freigabe durch den Orthopäden.",
            "plan": "Nach Freigabe: Laufband 10 Min. locker. Noch KEINE Sprünge mit Richtungswechsel (siehe Einschränkung)."}),
    )
    for key, appt_key, author, sections in linked:
        with practice_scope(demo.practice_id, u[author]):
            nid = nrepo.create(db, p[key], sections, appointment_id=demo.appointments[appt_key], source="dictation",
                               session_date=appt_day(appt_key), author_id=u[author])
            nrepo.approve(db, nid)
    with practice_scope(demo.practice_id, u["td"]):
        nid = nrepo.create(db, p["ayse"], {
            "befund": "Erstbefund mit Dolmetscherin (Türkisch). Lumbalgie seit Monaten mit Ausstrahlung links bis Knie. "
                      "Nachtschmerz, Morgensteifigkeit. Keine neurologischen Ausfälle, keine Blasen-/Mastdarmstörung angegeben. VAS 7/10.",
            "behandlung": "Instruktion Entlastungsstellungen, segmentale Stabilisation, Edukation (mit Bildern).",
            "beurteilung": "Unspezifische Lumbalgie mit pseudoradikulärer Ausstrahlung wahrscheinlich; Verlauf beobachten.",
            "plan": "Heimprogramm auf Türkisch über die App, Kontrolle in 1 Woche."}, kind="initial", session_date=demo.iso(-5),
            author_id=u["td"])
        nrepo.approve(db, nid)
        nid = nrepo.create(db, p["peter"], {
            "befund": "Knie links: Flexion 90°, Extension -5°, Schwellung mässig. Gang mit einem Stock.",
            "behandlung": "Lymphdrainage, Mobilisation, Treppentraining.", "beurteilung": "Fortschritte, Schwellung weiter beobachten.",
            "plan": "Treppentraining steigern."}, session_date=demo.iso(-7), author_id=u["td"])
        nrepo.approve(db, nid)


# --------------------------------------------------------------------------- Heimprogramm (mit Demo-Übersetzungen)
EX_TR = {
    "Beckenkippen": ("Leğen devirme",
                     "Sırtüstü yatın, dizleriniz bükük. Belinizin alt kısmını yavaşça yere bastırın ve tekrar gevşetin. Sakin nefes almaya devam edin."),
    "Katze und Kuh": ("Kedi ve inek",
                      "Dört ayak üzerinde sırtınızı yavaşça yuvarlayın (baş aşağıda), sonra hafifçe çukurlaştırın (bakış öne)."),
    "Brücke": ("Köprü",
               "Sırtüstü yatın, dizleriniz bükük. Kalçanızı sıkın ve uyluklarınızla gövdeniz bir çizgi olana kadar kalçanızı kaldırın. Yavaşça indirin."),
    "Spazieren gehen": ("Yürüyüş", "Her gün rahat bir tempoda yürüyün, süreyi yavaş yavaş artırın. İyi ayakkabı giyin."),
}
EX_PT = {
    "Pendelübung": ("Exercício de pêndulo",
                    "Apoie-se numa mesa com o braço são e incline o tronco para a frente. Deixe o braço afetado pendurado e faça pequenos círculos como um pêndulo."),
    "Schulterblätter zusammenziehen": ("Aproximar as omoplatas",
                                       "Sente-se direito/a. Aproxime as omoplatas para trás e para baixo, sem levantar os ombros. Mantenha e relaxe."),
    "Aussenrotation mit Band": ("Rotação externa com elástico",
                                "Cotovelos junto ao corpo, dobrados a 90°, elástico nas duas mãos. Rode lentamente os antebraços para fora, com os cotovelos junto ao corpo. Volte devagar."),
}
NOTE_TR = ("Nur so weit, wie es nicht ins Bein ausstrahlt.", "Sadece bacağınıza vurmayacak kadar yapın.")


@step
def homeprogram(app, demo: Demo) -> None:
    from datetime import date as _date

    from ..ai.translate import store_translation
    from ..core.db import utcnow
    from ..core.tenant import practice_scope
    from ..modules.homeprogram import repo as hrepo
    from ..modules.homeprogram.views import ensure_library

    db = demo.db
    ensure_library(db, demo.practice_id)
    by_code = {r["code"]: r for r in db.query("SELECT * FROM exercises WHERE practice_id = ?", (demo.practice_id,))}
    plans = {
        "lea": [("quad_set", None), ("heel_slide", None), ("mini_squat", "Nur bis 45° beugen, Knie über den Füssen.")],
        "ayse": [("pelvic_tilt", None), ("cat_cow", NOTE_TR[0]), ("bridge", None), ("walk", None)],
        "maria": [("pendulum", None), ("scap_retraction", None), ("er_band", None)],
        "peter": [("heel_slide", None), ("calf_raise", None), ("walk", None)],
    }
    with practice_scope(demo.practice_id, demo.users["td"]):
        for key, items in plans.items():
            prog = hrepo.current_program(db, demo.patients[key], create=True)
            ids = [hrepo.add_item(db, prog, by_code[c]["id"], note, "de") for c, note in items]
            hrepo.approve(db, prog)
            # Erledigt-Protokoll der letzten Tage
            for back, n in ((6, len(ids)), (5, len(ids)), (4, 0), (3, len(ids)), (2, len(ids) - 1), (1, len(ids)), (0, 1)):
                for item_id in ids[:n]:
                    db.execute("INSERT INTO exercise_logs (practice_id, patient_id, item_id, done_on, pain, created_at) VALUES (?,?,?,?,?,?)",
                               (demo.practice_id, demo.patients[key], item_id, (demo.today - timedelta(days=back)).isoformat(),
                                3 if key == "lea" else (6 if key == "ayse" else 4), utcnow()))
            # «Bei wenig Zeit zuerst» und ein Grund fürs Nicht-Üben (Tag ohne Übungen)
            if key in ("lea", "ayse"):
                hrepo.set_essential(db, ids[0], True)
            if key == "lea":
                hrepo.set_essential(db, ids[1], True)
                hrepo.log_skip(db, demo.patients[key], "no_time", "Spätdienst, erst um 23 Uhr zu Hause",
                               (demo.today - timedelta(days=4)).isoformat())
            if key == "ayse":
                hrepo.log_skip(db, demo.patients[key], "unclear", None, (demo.today - timedelta(days=4)).isoformat())
    for name_de, (name, text) in EX_TR.items():
        ex = next(r for r in by_code.values() if r["name_de"] == name_de)
        store_translation(db, demo.practice_id, ex["name_de"], "de", "tr", name, "demo")
        store_translation(db, demo.practice_id, ex["instructions_de"], "de", "tr", text, "demo")
    store_translation(db, demo.practice_id, NOTE_TR[0], "de", "tr", NOTE_TR[1], "demo")
    for name_de, (name, text) in EX_PT.items():
        ex = next(r for r in by_code.values() if r["name_de"] == name_de)
        store_translation(db, demo.practice_id, ex["name_de"], "de", "pt", name, "demo")
        store_translation(db, demo.practice_id, ex["instructions_de"], "de", "pt", text, "demo")
    _ = _date


# --------------------------------------------------------------------------- App-Zugang, Anliegen, Nachrichten
@step
def patient_side(app, demo: Demo) -> None:
    from ..core.db import utcnow
    from ..core.tenant import practice_scope
    from ..core.timeutil import local_dt, to_utc_iso

    from ..ai.translate import store_translation

    db = demo.db
    p = demo.patients
    # Ziel von Ayşe (in ihren Worten, Türkisch) – gespeicherte Demo-Übersetzung für den Kurzüberblick
    store_translation(db, demo.practice_id, "Gece ağrısız uyumak", "tr", "de", "Nachts ohne Schmerzen schlafen", "demo")
    store_translation(db, demo.practice_id, "Gece ağrısız uyumak", "tr", "fr", "Dormir la nuit sans douleur", "demo")
    for key in ("lea", "maria", "ayse"):
        for kind in ("app", "translation"):
            db.execute("INSERT INTO patient_consents (practice_id, patient_id, kind, version, accepted_at) VALUES (?,?,?,?,?)",
                       (demo.practice_id, p[key], kind, "2026-10", utcnow()))

    def at(days, hhmm):
        return to_utc_iso(local_dt(demo.d(days), hhmm))

    # Anliegen (strukturierte Antworten sind Codes; Freitext mit gespeicherter Demo-Übersetzung)
    db.execute(
        "INSERT INTO intakes (practice_id, patient_id, channel, lang, regions, pain, since, worse, goals, text_original,"
        " text_translated, translated_to, translation_engine, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (demo.practice_id, p["ayse"], "app", "tr", "lower_back_l,gluteal_l,hamstring_l", 8, "months", "night,sitting,morning",
         "sleep,work",
         "Belim çok ağrıyor, sol bacağıma kadar vuruyor. Geceleri uyuyamıyorum, sabahları çok sertleşiyor. "
         "Temizlik işinde çalışıyorum, eğilmek çok zor.",
         "Mein Rücken tut sehr weh, es strahlt bis in mein linkes Bein aus. Nachts kann ich nicht schlafen, morgens ist es "
         "sehr steif. Ich arbeite in der Reinigung, Bücken ist sehr schwer.", "de", "demo", "new", at(0, "09:24")))
    db.execute(
        "INSERT INTO intakes (practice_id, patient_id, channel, lang, regions, pain, since, worse, goals, text_original,"
        " text_translated, translated_to, translation_engine, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (demo.practice_id, p["maria"], "app", "pt", "front_deltoids_l,back_deltoids_l", 6, "weeks", "night,move", "overhead,household",
         "Não consigo dormir sobre o ombro esquerdo. Quando levanto o braço para pendurar a roupa dói muito.",
         "Ich kann nicht auf der linken Schulter schlafen. Wenn ich den Arm hebe, um die Wäsche aufzuhängen, tut es sehr weh.",
         "de", "demo", "new", at(-1, "19:40")))
    # Nachrichten
    with practice_scope(demo.practice_id):
        db.execute("INSERT INTO messages (practice_id, patient_id, direction, kind, body, lang, region, pain, created_at)"
                   " VALUES (?,?,?,?,?,?,?,?,?)",
                   (demo.practice_id, p["lea"], "in", "problem",
                    "Seit gestern Abend ist das Knie nach dem Treppensteigen etwas geschwollen. Soll ich die Übungen trotzdem machen?",
                    "de", "knees_r", 4, at(-1, "21:14")))
        db.execute("INSERT INTO messages (practice_id, patient_id, direction, body, lang, translation, translation_lang,"
                   " translation_engine, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (demo.practice_id, p["luca"], "in", "Buongiorno, il medico mi ha permesso di correre. Posso già correre fuori sull'asfalto?",
                    "it", "Guten Tag, der Arzt hat mir das Joggen erlaubt. Darf ich schon draussen auf Asphalt joggen?", "de", "demo",
                    at(-1, "19:05")))
        db.execute("INSERT INTO messages (practice_id, patient_id, direction, body, lang, translation, translation_lang,"
                   " translation_engine, read_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (demo.practice_id, p["maria"], "in", "Bom dia, posso fazer os exercícios duas vezes por dia?", "pt",
                    "Guten Tag, darf ich die Übungen zweimal pro Tag machen?", "de", "demo", at(-2, "09:00"), at(-2, "08:12")))
        db.execute("INSERT INTO messages (practice_id, patient_id, direction, author_user_id, body, lang, translation, translation_lang,"
                   " translation_engine, read_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (demo.practice_id, p["maria"], "out", demo.users["td"],
                    "Guten Tag Frau Exemple, ja, zweimal täglich ist gut – aber nur so weit, dass es nicht stark schmerzt.", "de",
                    "Bom dia, Sra. Exemple, sim, duas vezes por dia está bem – mas só até onde não doer muito.", "pt", "demo",
                    at(-2, "12:30"), at(-2, "10:05")))


# --------------------------------------------------------------------------- Information für Patient/innen (übersetzt, vorlesbar)
PI_AYSE = {
    "home": ("Machen Sie die Übungen aus der App zweimal am Tag – nur so weit, wie es nicht ins Bein ausstrahlt. "
             "Stehen Sie bei der Arbeit öfter kurz auf und gehen Sie ein paar Schritte."),
    "contact": ("Melden Sie sich bei uns, wenn das Bein taub oder schwach wird. Gehen Sie sofort zum Arzt, wenn Sie "
                "Probleme beim Wasserlassen haben. Im Notfall rufen Sie 144 an."),
    "home_tr": ("Uygulamadaki egzersizleri günde iki kez yapın – yalnızca bacağınıza vurmayacak kadar. "
                "İşte sık sık kısa süre ayağa kalkın ve birkaç adım yürüyün."),
    "contact_tr": ("Bacağınız uyuşursa veya güçsüzleşirse bize haber verin. İdrar yaparken sorun yaşarsanız hemen doktora "
                   "gidin. Acil durumda 144'ü arayın."),
}


@step
def patient_info(app, demo: Demo) -> None:
    from ..core.tenant import practice_scope
    from ..modules.patientinfo import repo as pirepo

    db = demo.db
    with practice_scope(demo.practice_id, demo.users["td"]):
        iid = pirepo.create_approved(db, demo.patients["ayse"], PI_AYSE["home"], PI_AYSE["contact"], "de", "tr")
        pirepo.save_translation(db, iid, PI_AYSE["home_tr"], PI_AYSE["contact_tr"], "demo", None)
        iid = pirepo.create_approved(db, demo.patients["lea"], "Kühlen Sie das Knie nach den Übungen 15 Minuten. "
                                     "Velofahren erst nach der Freigabe durch den Operateur.",
                                     "Melden Sie sich, wenn das Knie heiss wird oder stark anschwillt.", "de", "de")
        pirepo.save_translation(db, iid, None, None, "identity", None)



# --------------------------------------------------------------------------- Abrechnung: Stammdaten, Zahlungskonto, eine bezahlte und eine offene Rechnung
def _demo_gln(n: int) -> str:
    """Fiktive GLN im Bereich 2099… (keine echte Firma/Person), Prüfziffer nach GS1."""
    body = f"2099990{n:05d}"
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return body + str((10 - total % 10) % 10)


@step
def billing(app, demo: Demo) -> None:
    from ..core.tenant import practice_scope
    from ..modules.billing import payment

    db = demo.db
    pid = demo.practice_id
    db.execute("UPDATE practices SET street = ?, house_no = ?, zip = ?, city = ?, gln = ?, zsr = ? WHERE id = ?",
               ("Demostrasse", "1", "2500", "Biel/Bienne", _demo_gln(1), "Z999990", pid))
    for i, key in enumerate(("owner", "td", "ab", "se", "le"), start=10):
        db.execute("UPDATE users SET gln = ?, zsr = ? WHERE id = ?", (_demo_gln(i), f"Z9999{i:02d}", demo.users[key]))
    for i, (name, ins_id) in enumerate(sorted(demo.insurers.items()), start=100):
        db.execute("UPDATE insurers SET gln = ?, street = ?, zip = ?, city = ? WHERE id = ?",
                   (_demo_gln(i), "Demoweg 1", "3000", "Bern (Demo)", ins_id))
    for i, (key, ref_id) in enumerate(sorted(demo.referrers.items()), start=200):
        db.execute("UPDATE referrers SET gln = ? WHERE id = ?", (_demo_gln(i), ref_id))
    for i, (key, p) in enumerate(sorted(demo.patients.items()), start=1):
        db.execute("UPDATE patients SET address = ? WHERE id = ?", (f"Musterweg {i}", p))
    # Offizielle Beispielnummer des BSV (fiktiv) – nur für Lea; bei den anderen zeigt N-Fizyo, was fürs XML fehlt.
    db.execute("UPDATE patients SET ahv_number = ? WHERE id = ?", ("7569217076985", demo.patients["lea"]))
    with practice_scope(pid, demo.users["owner"]):
        # Beispiel-QR-IBAN aus den Swiss Payment Standards (SIX) – fiktiv
        payment.save_settings(db, {"iban": "CH4431999123000889012", "payment_days": 30, "reminder_days": 10,
                                   "reminder_fee_2": 10.0, "reminder_fee_3": 20.0, "xml_modus": "test", "copy_to_insurer": 1})
        # Taxpunktwert KVG: Demo-Wert (fiktiv) – in der echten Praxis gilt der kantonale bzw. vertragliche Wert
        from ..modules.billing import repo as brepo

        brepo.set_tp_value(db, "kvg312", 1.00, "2018-01-01", "Demo-Wert, nicht amtlich")


@step
def cashdesk(app, demo: Demo) -> None:
    """Beispiel-Preisliste (fiktive Preise), ein MTT-Abo mit Besuchen, ein abgeschlossener Kassentag."""
    from ..core import patients as core_patients
    from ..core.tenant import practice_scope
    from ..modules.billing import private
    from ..modules.cashdesk import repo as cash

    db = demo.db
    ty = demo.types
    items = (
        ("service", "PRIV-30", "Physiotherapie privat (30 Min.)", "Physiothérapie privée (30 min)", 85.00, 0.0, ty["physio"], None, None),
        ("service", "PRIV-45", "Physiotherapie privat (45 Min.)", "Physiothérapie privée (45 min)", 120.00, 0.0, ty["complex"], None, None),
        ("service", "LYMPH-P", "Lymphdrainage privat (45 Min.)", "Drainage lymphatique privé (45 min)", 115.00, 0.0, ty["lymph"], None, None),
        ("service", "PRIV", "Physiotherapie privat", "Physiothérapie privée", 95.00, 0.0, None, None, None),
        ("product", "TB-ROT", "Theraband rot (2,5 m)", "Theraband rouge (2,5 m)", 12.90, 8.1, None, None, None),
        ("product", "TB-GRUEN", "Theraband grün (2,5 m)", "Theraband vert (2,5 m)", 14.90, 8.1, None, None, None),
        ("product", "TAPE", "Kinesio-Tape (5 m)", "Kinesio-tape (5 m)", 16.50, 8.1, None, None, None),
        ("product", "ROLLE", "Faszienrolle", "Rouleau de massage", 34.90, 8.1, None, None, None),
        ("abo", "MTT-10", "MTT-Abo 10 Eintritte", "Abonnement MTT 10 entrées", 220.00, 8.1, None, 10, 180),
        ("abo", "MTT-6M", "MTT-Abo 6 Monate", "Abonnement MTT 6 mois", 390.00, 8.1, None, None, 183),
    )
    with practice_scope(demo.practice_id, demo.users["owner"]):
        for kind, code, de, fr, price, vat, type_id, sessions, days in items:
            private.save_item(db, {"kind": kind, "code": code, "name_de": de, "name_fr": fr, "price_chf": price, "vat_rate": vat,
                                   "treatment_type_id": type_id, "abo_sessions": sessions, "abo_days": days})
    with practice_scope(demo.practice_id, demo.users["ab"]):
        sandra = core_patients.get(db, demo.patients["sandra"])
        abo = private.item_by_code(db, "MTT-10")
        band = private.item_by_code(db, "TB-ROT")
        day = demo.iso(-20)
        sale_id, _ = cash.sell(db, patient=sandra, entries=[(abo, 1), (band, 1)], method="twint", lang="de", day=day)
        sub_id = db.value("SELECT id FROM subscriptions WHERE sale_id = ?", (sale_id,))
        for offset in (-20, -17, -13, -10, -6, -3):
            db.execute("INSERT INTO subscription_visits (practice_id, subscription_id, visited_on, created_by, created_at)"
                       " VALUES (?,?,?,?,?)", (demo.practice_id, sub_id, demo.iso(offset), demo.users["ab"], demo.iso(offset) + "T09:00:00+00:00"))
        cash.close_day(db, day, 0.0, None)


@step
def mtt(app, demo: Demo) -> None:
    """MTT-Fläche: Geräte (Standort Sport), Luca trainiert nach Plan (Verlauf → Vorschlag zur Steigerung) und ist gerade
    auf der Fläche; Sandra trainiert mit Abo (zählt per Check-in)."""
    from datetime import datetime, timedelta, timezone

    from ..core.tenant import practice_scope
    from ..modules.cashdesk import api as cash_api
    from ..modules.mtt import repo as mtt

    db = demo.db
    sport = demo.locations.get("sport")
    with practice_scope(demo.practice_id, demo.users["le"]):
        m = {key: mtt.add_machine(db, name, kind, step, sport) for key, name, kind, step in (
            ("press", "Beinpresse", "strength", 2.5), ("curl", "Beinbeuger", "strength", 2.5), ("ext", "Kniestrecker", "strength", 2.5),
            ("lat", "Latzug", "strength", 2.5), ("row", "Rudern sitzend", "strength", 2.5), ("bike", "Ergometer", "cardio", 1),
            ("tread", "Laufband", "cardio", 1), ("balance", "Balance-Pad", "free", 1))}
        luca = demo.patients["luca"]
        plan = mtt.current_plan(db, luca, create=True)
        items = {}
        for key, f in (("bike", {"minutes": 10, "setting": "Stufe 6"}), ("press", {"sets": 3, "reps": 12, "load": 40.0, "setting": "Sitz 5"}),
                       ("ext", {"sets": 3, "reps": 12, "load": 15.0, "setting": "Lehne 3"}), ("curl", {"sets": 3, "reps": 12, "load": 20.0}),
                       ("balance", {"sets": 3, "reps": 30, "note": "Einbeinig links, 30 Sekunden"})):
            iid = mtt.add_item(db, plan, mtt.machine(db, m[key]))
            mtt.update_item(db, iid, **f)
            items[key] = iid
        mtt.approve(db, plan, "Wieder Fussball spielen – Kraft und Stabilität links")
        from ..ai.translate import store_translation

        store_translation(db, demo.practice_id, "Wieder Fussball spielen – Kraft und Stabilität links", "de", "it",
                          "Tornare a giocare a calcio – forza e stabilità a sinistra", "demo")
        store_translation(db, demo.practice_id, "Einbeinig links, 30 Sekunden", "de", "it", "Su una gamba sola, sinistra, 30 secondi", "demo")
        # Verlauf: Beinpresse von 30 auf 40 kg, die letzten zwei Male leicht → Vorschlag 42,5 kg
        history = ((-21, 30.0, 6, 2), (-18, 32.5, 6, 2), (-14, 35.0, 6, 1), (-11, 37.5, 7, 2), (-7, 40.0, 5, 1), (-4, 40.0, 5, 1))
        for offset, press, effort, pain in history:
            day = demo.iso(offset)
            vid = db.execute("INSERT INTO mtt_visits (practice_id, patient_id, plan_id, day, started_at, ended_at, source) VALUES (?,?,?,?,?,?,?)",
                             (demo.practice_id, luca, plan["id"], day, day + "T12:35:00+00:00", day + "T13:30:00+00:00", "qr"))
            rows = [(items["bike"], None, None, 10, 4, 0)]
            rows += [(items["press"], 12, press, None, effort, pain)] * 3
            rows += [(items["ext"], 12 if offset < -7 else 11, 15.0, None, 7, 2)] * 3
            rows += [(items["curl"], 12, 20.0, None, 6, 1)] * 3
            for item_id, reps, load, minutes, eff, pn in rows:
                db.execute("INSERT INTO mtt_sets (practice_id, visit_id, item_id, patient_id, reps, load, minutes, effort, pain, created_at)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?)", (demo.practice_id, vid, item_id, luca, reps, load, minutes, eff, pn, day + "T13:00:00+00:00"))
        for key in ("press", "ext", "curl"):
            mtt.refresh_suggestion(db, mtt.item(db, items[key]))
        # Heute: Luca ist gerade auf der Fläche (per QR-Code eingecheckt)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        today = demo.today.isoformat()
        vid = db.execute("INSERT INTO mtt_visits (practice_id, patient_id, plan_id, day, started_at, source) VALUES (?,?,?,?,?,?)",
                         (demo.practice_id, luca, plan["id"], today, (now - timedelta(minutes=22)).isoformat(), "qr"))
        for item_id, reps, load, minutes, eff, pn, ago in ((items["bike"], None, None, 10, 4, 0, 18), (items["press"], 12, 40.0, None, 5, 1, 9),
                                                          (items["press"], 12, 40.0, None, 5, 2, 6)):
            db.execute("INSERT INTO mtt_sets (practice_id, visit_id, item_id, patient_id, reps, load, minutes, effort, pain, created_at)"
                       " VALUES (?,?,?,?,?,?,?,?,?,?)", (demo.practice_id, vid, item_id, luca, reps, load, minutes, eff, pn,
                                                         (now - timedelta(minutes=ago)).isoformat()))
    # Sandra: Selbstzahlerin mit MTT-Abo (Plan von A. Beispiel), heute per QR eingecheckt → zählt als Eintritt
    with practice_scope(demo.practice_id, demo.users["ab"]):
        sandra = demo.patients["sandra"]
        plan = mtt.current_plan(db, sandra, create=True)
        for key, f in (("lat", {"sets": 3, "reps": 15, "load": 25.0}), ("row", {"sets": 3, "reps": 15, "load": 20.0}),
                       ("tread", {"minutes": 15})):
            mtt.update_item(db, mtt.add_item(db, plan, mtt.machine(db, m[key])), **f)
        mtt.approve(db, plan, "Ganzer Arbeitstag am PC ohne Nackenschmerzen")
        db.execute("INSERT INTO mtt_visits (practice_id, patient_id, plan_id, day, started_at, source) VALUES (?,?,?,?,?,?)",
                   (demo.practice_id, sandra, plan["id"], demo.today.isoformat(), (now - timedelta(minutes=12)).isoformat(), "qr"))
        cash_api.checkin_visit(db, sandra, demo.today.isoformat())


@step
def video(app, demo: Demo) -> None:
    """Ayşe hat morgen um 18 Uhr eine Video-Sitzung bei T. Demo (Link per SMS gesendet, Einwilligung fehlt noch)."""
    from datetime import timedelta

    from ..core import access
    from ..core.security import new_token, token_hash
    from ..core.tenant import practice_scope
    from ..core.timeutil import local_dt
    from ..modules.agenda import repo as arepo
    from ..modules.video import repo as vrepo

    db = demo.db
    day = demo.d(1)
    if day.weekday() >= 5:
        day += timedelta(days=7 - day.weekday())
    with practice_scope(demo.practice_id, demo.users["td"]):
        ayse = demo.patients["ayse"]
        aid = arepo.create(db, therapist_id=demo.users["td"], start=local_dt(day, "18:00"), minutes=30, patient_id=ayse,
                           location_id=demo.locations["bahnhof"], treatment_type_id=demo.types["physio"],
                           prescription_id=demo.prescriptions.get("ayse"), note="Video: Kontrolle Heimprogramm")
        access.add_to_team(db, ayse, demo.users["td"], "appointment")
        rid = vrepo.create_room(db, aid, ayse)
        db.execute("UPDATE video_rooms SET link_hash = ?, sms_sent_at = ? WHERE id = ?", (token_hash(new_token(24)), demo.iso(0) + "T08:05:00+00:00", rid))
        demo.appointments["ayse_video"] = aid


# Online-Termine: gespeicherte Demo-Übersetzungen (im Betrieb übersetzt die KI der Praxis im Hintergrund)
BOOKING_TEXTS = {
    "Neue Patientinnen und Patienten: bitte die ärztliche Verordnung zum ersten Termin mitbringen.": {
        "it": "Nuovi pazienti: portate la prescrizione medica al primo appuntamento.",
        "en": "New patients: please bring your doctor's prescription to the first appointment.",
        "pt": "Novos pacientes: tragam a prescrição médica para a primeira consulta.",
        "es": "Pacientes nuevos: traigan la prescripción médica a la primera cita.",
        "sq": "Pacientët e rinj: ju lutemi sillni recetën e mjekut në takimin e parë.",
        "bks": "Novi pacijenti: molimo ponesite ljekarski nalog na prvi termin.",
        "tr": "Yeni hastalar: lütfen ilk randevuya doktorunuzun sevk belgesini getirin.",
        "ar": "المرضى الجدد: يرجى إحضار الوصفة الطبية في الموعد الأول.",
        "uk": "Нові пацієнти: будь ласка, візьміть направлення лікаря на перший прийом.",
    },
    "Physiotherapie": {
        "it": "Fisioterapia", "en": "Physiotherapy", "pt": "Fisioterapia", "es": "Fisioterapia", "sq": "Fizioterapi",
        "bks": "Fizioterapija", "tr": "Fizyoterapi", "ar": "العلاج الطبيعي", "uk": "Фізіотерапія",
    },
    "Erstbehandlung mit Befund": {
        "it": "Primo trattamento con valutazione", "en": "First treatment with assessment",
        "pt": "Primeiro tratamento com avaliação", "es": "Primer tratamiento con evaluación",
        "sq": "Trajtimi i parë me vlerësim", "bks": "Prvi tretman s pregledom", "tr": "Muayeneli ilk tedavi",
        "ar": "العلاج الأول مع الفحص", "uk": "Перше лікування з обстеженням",
    },
    "Medizinische Trainingstherapie": {
        "it": "Terapia di allenamento medico", "en": "Medical training therapy", "pt": "Terapia de treino médico",
        "es": "Terapia de entrenamiento médico", "sq": "Terapi me stërvitje mjekësore",
        "bks": "Medicinska terapija vježbanjem", "tr": "Tıbbi egzersiz tedavisi", "ar": "العلاج بالتمارين الطبية",
        "uk": "Медична тренувальна терапія",
    },
}


@step
def booking(app, demo: Demo) -> None:
    """Online-Termine eingeschaltet (Physiotherapie, Erstbehandlung, MTT) und eine offene Anfrage (fiktive Person)."""
    from datetime import datetime, timedelta

    from ..core.tenant import practice_scope
    from ..core.timeutil import TZ
    from ..modules.booking import repo as booking

    db = demo.db
    with practice_scope(demo.practice_id, demo.users["owner"]):
        booking.save_settings(db, {"enabled": 1, "days_ahead": 21, "lead_hours": 12, "slot_step": 15, "show_therapists": 0,
                                   "intro_de": "Neue Patientinnen und Patienten: bitte die ärztliche Verordnung zum ersten Termin mitbringen.",
                                   "intro_fr": "Nouveaux patients : merci d’apporter la prescription médicale au premier rendez-vous."},
                              [demo.types["physio"], demo.types["first"], demo.types["mtt"]], [demo.users["td"], demo.users["ab"]])
        from ..ai.translate import store_translation

        for source, by_lang in BOOKING_TEXTS.items():
            for target, text in by_lang.items():
                store_translation(db, demo.practice_id, source, "de", target, text, "demo")
        types = {t["id"]: t for t in booking.bookable_types(db)}
        start = (datetime.now(TZ) + timedelta(days=3)).replace(hour=16, minute=30, second=0, microsecond=0)
        while start.weekday() >= 5:      # Arbeitstag
            start += timedelta(days=1)
        rid = booking.create_request(db, type_row=types[demo.types["first"]], therapist_id=demo.users["ab"], start=start,
                                     location_id=demo.locations["altstadt"],
                                     data={"first_name": "Nora", "last_name": "Beispiel", "birthdate": "1990-05-14", "phone": "+41790000099",
                                           "email": "nora.beispiel@example.invalid", "email_ok": True, "lang": "de", "insurance": "KVG",
                                           "has_prescription": True, "note": "Schulter rechts, Verordnung vom Hausarzt"})
        db.execute("UPDATE booking_requests SET status = 'requested', verified_at = created_at WHERE id = ?", (rid,))


@step
def firms(app, demo: Demo) -> None:
    """Firmenkunden (fiktiv): eine abgeschlossene Rechnung für eine Rückenschule im Betrieb, ein Entwurf mit MTT-Abos."""
    from ..core.tenant import practice_scope
    from ..modules.billing import private
    from ..modules.firms import repo as frepo

    db = demo.db
    with practice_scope(demo.practice_id, demo.users["owner"]):
        f1 = frepo.save_firm(db, {"name": "Beispiel Präzision AG (Demo)", "department": "Personalabteilung", "contact": "Frau R. Beispiel",
                                  "street": "Industriestrasse", "house_no": "12", "zip": "2504", "city": "Biel/Bienne",
                                  "email": "hr@beispiel-praezision.example", "phone": "032 000 00 70", "uid": None,
                                  "their_reference": "Bestellung 2026-118", "payment_days": 30, "lang": "de", "note": "Fiktive Firma (Demo)"})
        f2 = frepo.save_firm(db, {"name": "Exemple Horlogerie SA (Démo)", "department": "Ressources humaines", "contact": "M. J. Exemple",
                                  "street": "Rue de l’Exemple", "house_no": "5", "zip": "2502", "city": "Biel/Bienne",
                                  "email": "rh@exemple-horlogerie.example", "phone": "032 000 00 71", "uid": None,
                                  "their_reference": None, "payment_days": None, "lang": "fr", "note": "Entreprise fictive (démo)"})
        inv = frepo.get(db, frepo.create(db, frepo.get_firm(db, f1), subject="Rückenschule im Betrieb, Oktober 2026"))
        frepo.update_head(db, inv, subject=inv["subject"], their_reference="Bestellung 2026-118", period_from=demo.iso(-30),
                          period_to=demo.iso(-9), lang="de")
        frepo.add_free_line(db, inv, label="Rückenschule im Betrieb (90 Minuten, Gruppe bis 12 Personen)", quantity=2, unit_price=360.0,
                            vat_rate=0.0, kind="service", service_date=demo.iso(-16))
        frepo.add_free_line(db, inv, label="Ergonomie-Check am Arbeitsplatz (pro Person)", quantity=6, unit_price=95.0, vat_rate=0.0,
                            kind="service", service_date=demo.iso(-9))
        frepo.finalize(db, frepo.get(db, inv["id"]))
        draft = frepo.get(db, frepo.create(db, frepo.get_firm(db, f2), subject="Abonnements MTT pour 3 collaborateurs"))
        abo = private.item_by_code(db, "MTT-10")
        if abo is not None:
            frepo.add_item_line(db, draft, abo, 3, "fr", demo.iso(0))
