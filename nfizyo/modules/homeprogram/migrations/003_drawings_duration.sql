-- Heimprogramm 0.9: Zeichnung pro Übung (Datei static/img/exercises/<drawing>.svg, leer = Körperbereich-Kachel)
-- und Dauer in Minuten (z. B. Spazieren 20 min) – in der Bibliothek und pro Übung im Programm.
ALTER TABLE exercises ADD COLUMN drawing TEXT;
ALTER TABLE exercises ADD COLUMN duration_min INTEGER;
ALTER TABLE program_items ADD COLUMN duration_min INTEGER;

-- Start-Bibliothek: jede Übung bekommt ihre eigene Zeichnung.
UPDATE exercises SET drawing = code WHERE code IN ('quad_set', 'heel_slide', 'mini_squat', 'slr', 'calf_raise', 'pendulum',
    'scap_retraction', 'er_band', 'wall_slide', 'pelvic_tilt', 'cat_cow', 'bridge', 'bird_dog', 'chin_tuck', 'shoulder_circles',
    'single_leg', 'ankle_abc', 'side_leg_raise', 'walk');

-- «Spazieren gehen» in der Bibliothek: Dauer statt «1 ×» (nur wenn der Vorschlag noch unverändert ist).
-- Bestehende, freigegebene Programme bleiben, wie die Therapeutin / der Therapeut sie freigegeben hat.
UPDATE exercises SET sets = NULL, duration_min = 20
 WHERE code = 'walk' AND sets = 1 AND reps IS NULL AND hold_sec IS NULL AND duration_min IS NULL;
