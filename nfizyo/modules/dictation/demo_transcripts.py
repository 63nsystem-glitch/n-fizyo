"""Demo-Transkripte: ersetzen die Spracherkennung NUR im Demo-Modus ohne installiertes Whisper.
Die Oberfläche kennzeichnet sie als "Demo – Spracherkennung simuliert"."""

DEMO = {
    "de": {
        "knee": ("Patientin berichtet, das Knie sei nach dem Treppensteigen gestern etwas geschwollen gewesen, heute weniger. "
                 "Schmerz VAS 3 von 10 bei Belastung. Flexion aktiv 105 Grad, Extension minus 5 Grad. Leichter Erguss rechts. "
                 "Behandlung: Patellamobilisation, Lymphdrainage 15 Minuten, Mini-Kniebeugen bis 60 Grad, Gangschule ohne Stöcke. "
                 "Beurteilung: Flexion gegenüber letzter Woche verbessert, Gangbild sicherer. "
                 "Plan: Velo-Ergometer ohne Widerstand gemäss Freigabe einführen, weiterhin keine offene Kette zwischen 90 und 45 Grad."),
        "shoulder": ("Patientin berichtet weniger Nachtschmerz. Schmerz VAS 4 von 10. Abduktion 100 Grad, Aussenrotation 35 Grad. "
                     "Behandlung: Mobilisation glenohumeral, Kräftigung Rotatorenmanschette mit Theraband, Haltungsschulung. "
                     "Beurteilung: Beweglichkeit leicht verbessert. Plan: Heimprogramm steigern, nächste Woche Kontrolle der Abduktion."),
        "lumbar": ("Patientin gibt an, nachts besser zu schlafen. Schmerz VAS 5 von 10, Ausstrahlung ins linke Bein bis zum Knie. "
                   "Behandlung: Instruktion Entlastungsstellung, segmentale Stabilisation, Gehtraining 10 Minuten. "
                   "Beurteilung: Schmerzen leicht rückläufig, keine neuen neurologischen Ausfälle. "
                   "Plan: Stabilisation fortsetzen, Heimprogramm mit Bildern in türkischer Sprache abgeben."),
        "default": ("Patient berichtet über weniger Beschwerden seit der letzten Sitzung. Schmerz VAS 3 von 10. "
                    "Behandlung: Mobilisation, Kräftigung und Instruktion Heimprogramm. "
                    "Beurteilung: Verlauf positiv. Plan: Therapie wie geplant fortsetzen."),
    },
    "fr": {
        "knee": ("La patiente rapporte un léger gonflement du genou hier après les escaliers, moins aujourd'hui. "
                 "Douleur EVA 3 sur 10 à la charge. Flexion active 105 degrés, extension moins 5 degrés. "
                 "Traitement : mobilisation de la rotule, drainage lymphatique 15 minutes, mini-squats jusqu'à 60 degrés. "
                 "Évaluation : flexion améliorée par rapport à la semaine passée. "
                 "Plan : introduire le vélo sans résistance, toujours pas de chaîne ouverte entre 90 et 45 degrés."),
        "shoulder": ("Le patient rapporte moins de douleurs nocturnes. Douleur EVA 4 sur 10. Élévation 110 degrés, rotation externe 30 degrés. "
                     "Traitement : mobilisation passive, renforcement de la coiffe sous le seuil douloureux. "
                     "Évaluation : évolution favorable. Plan : poursuivre, contrôle chez le médecin dans 2 semaines."),
        "lumbar": ("La patiente dort mieux. Douleur EVA 5 sur 10 avec irradiation dans la jambe gauche. "
                   "Traitement : stabilisation segmentaire, marche 10 minutes. Évaluation : légère amélioration. Plan : poursuivre."),
        "default": ("Le patient rapporte moins de plaintes. Douleur EVA 3 sur 10. Traitement : mobilisation et renforcement. "
                    "Évaluation : évolution favorable. Plan : poursuivre le traitement."),
    },
}


def pick(lang: str, joint: str | None) -> str:
    texts = DEMO.get(lang, DEMO["de"])
    key = {"knee": "knee", "shoulder": "shoulder", "sij": "lumbar"}.get(joint or "", None)
    if joint in ("cervical", "lumbar"):
        key = "lumbar"
    return texts.get(key or "default", texts["default"])
