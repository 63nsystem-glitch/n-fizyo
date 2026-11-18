"""Erzeugt das schematische Skelett (vorne/hinten) für N-Fizyo.

Eigene Zeichnung (NSystem), keine fremden Vorlagen. Ausgabe: skeleton.json mit
Elementen {id, kind, o:[shapes], f:[shapes]}; o = Umriss-Ebene, f = Füll-Ebene.
Koordinaten: viewBox -8 -2 116 206. Vorne: Patient rechts = Betrachter links.
"""
import json
import math
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "nfizyo/knowledge/data/skeleton.json"


def f(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def P(d):
    return {"t": "path", "d": d}


def C(cx, cy, r):
    return {"t": "circle", "cx": f(cx), "cy": f(cy), "r": f(r)}


def E(cx, cy, rx, ry, rot=0):
    s = {"t": "ellipse", "cx": f(cx), "cy": f(cy), "rx": f(rx), "ry": f(ry)}
    if rot:
        s["transform"] = f"rotate({f(rot)} {f(cx)} {f(cy)})"
    return s


def R(x, y, w, h, rx):
    return {"t": "rect", "x": f(x), "y": f(y), "width": f(w), "height": f(h), "rx": f(rx)}


def long_bone(p1, p2, wt, w, wb, tl=0.13, bl=0.15):
    """Röhrenknochen von p1 nach p2 mit Gelenkenden (Breiten wt/wb) und Schaft (w)."""
    (x1, y1), (x2, y2) = p1, p2
    L = math.hypot(x2 - x1, y2 - y1)
    ux, uy = (x2 - x1) / L, (y2 - y1) / L
    nx, ny = -uy, ux

    def pt(u, v):
        return (x1 + ux * u + nx * v, y1 + uy * u + ny * v)

    a, b = tl * L, (1 - bl) * L
    # linke Seite (v negativ) von oben nach unten
    d = []
    p = pt(0, -wt / 2)
    d.append(f"M{f(p[0])},{f(p[1])}")
    c1, c2, e = pt(a * 0.4, -wt / 2), pt(a * 0.6, -w / 2), pt(a, -w / 2)
    d.append(f"C{f(c1[0])},{f(c1[1])} {f(c2[0])},{f(c2[1])} {f(e[0])},{f(e[1])}")
    e = pt(b, -w / 2)
    d.append(f"L{f(e[0])},{f(e[1])}")
    c1, c2, e = pt(b + (L - b) * 0.4, -w / 2), pt(b + (L - b) * 0.6, -wb / 2), pt(L, -wb / 2)
    d.append(f"C{f(c1[0])},{f(c1[1])} {f(c2[0])},{f(c2[1])} {f(e[0])},{f(e[1])}")
    # unteres Ende rund
    c1, c2, e = pt(L + wb * 0.45, -wb / 2), pt(L + wb * 0.45, wb / 2), pt(L, wb / 2)
    d.append(f"C{f(c1[0])},{f(c1[1])} {f(c2[0])},{f(c2[1])} {f(e[0])},{f(e[1])}")
    c1, c2, e = pt(b + (L - b) * 0.6, wb / 2), pt(b + (L - b) * 0.4, w / 2), pt(b, w / 2)
    d.append(f"C{f(c1[0])},{f(c1[1])} {f(c2[0])},{f(c2[1])} {f(e[0])},{f(e[1])}")
    e = pt(a, w / 2)
    d.append(f"L{f(e[0])},{f(e[1])}")
    c1, c2, e = pt(a * 0.6, w / 2), pt(a * 0.4, wt / 2), pt(0, wt / 2)
    d.append(f"C{f(c1[0])},{f(c1[1])} {f(c2[0])},{f(c2[1])} {f(e[0])},{f(e[1])}")
    c1, c2, e = pt(-wt * 0.45, wt / 2), pt(-wt * 0.45, -wt / 2), pt(0, -wt / 2)
    d.append(f"C{f(c1[0])},{f(c1[1])} {f(c2[0])},{f(c2[1])} {f(e[0])},{f(e[1])}Z")
    return P(" ".join(d))


def capsule(p1, p2, w):
    return long_bone(p1, p2, w, w * 0.78, w, tl=0.25, bl=0.25)


def hand(wrist, elbow, side_sign, palm_front=True):
    """Hand als Handwurzel + 5 Strahlen. side_sign: -1 = Daumen zur kleineren x-Seite."""
    dx, dy = wrist[0] - elbow[0], wrist[1] - elbow[1]
    L = math.hypot(dx, dy)
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    if nx * side_sign < 0:
        nx, ny = -nx, -ny  # n zeigt zur Daumenseite
    cx, cy = wrist[0] + ux * 2.0, wrist[1] + uy * 2.0
    shapes = [E(cx, cy, 2.6, 1.9, math.degrees(math.atan2(uy, ux)) + 90)]
    for i in range(5):  # 0 = Daumen
        off = (2 - i) * 1.15
        spread = (2 - i) * 0.09
        if i == 0:
            off, spread = 2.5, 0.32
        bx, by = cx + ux * 1.6 + nx * off, cy + uy * 1.6 + ny * off
        dirx, diry = ux + nx * spread, uy + ny * spread
        dl = math.hypot(dirx, diry)
        dirx, diry = dirx / dl, diry / dl
        ml = 3.2 if i == 0 else 4.6 - abs(i - 2) * 0.35
        fl = 3.2 if i == 0 else 5.2 - abs(i - 2) * 0.55 - (0.6 if i == 4 else 0)
        mw = 1.05 if i == 0 else 0.9
        ex, ey = bx + dirx * ml, by + diry * ml
        shapes.append(capsule((bx, by), (ex, ey), mw))
        gx, gy = ex + dirx * 0.45, ey + diry * 0.45
        shapes.append(capsule((gx, gy), (gx + dirx * fl, gy + diry * fl), mw * 0.82))
    return shapes


def mirror_shape(s):
    s = dict(s)
    if s["t"] == "path":
        import re

        def mx(m):
            x, y = float(m.group(1)), m.group(2)
            return f"{f(100 - x)},{y}"

        s["d"] = re.sub(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", mx, s["d"])
    elif s["t"] in ("circle", "ellipse"):
        s["cx"] = f(100 - float(s["cx"]))
        if "transform" in s:
            ang = float(s["transform"].split("(")[1].split()[0])
            s["transform"] = f"rotate({f(-ang)} {s['cx']} {s['cy']})"
    elif s["t"] == "rect":
        s["x"] = f(100 - float(s["x"]) - float(s["width"]))
    return s


def el(id_, kind, shapes, details=None):
    return {"id": id_, "kind": kind, "shapes": shapes, "details": details or []}


def mirror_el(e, new_id):
    return {"id": new_id, "kind": e["kind"], "shapes": [mirror_shape(s) for s in e["shapes"]],
            "details": [mirror_shape(s) for s in e["details"]]}


# ------------------------------------------------------------------ Geometrie (rechte Körperseite, vorne)
SHOULDER = (27.6, 42.8)
ELBOW = (22.4, 70.6)
WRIST = (14.0, 98.4)
HIP = (42.2, 96.6)
KNEE = (32.6, 146.6)
ANKLE = (31.4, 187.4)


def right_side_front():
    els = []
    els.append(el("clavicle_r", "bone", [P(
        "M48.6,39.2 C44.6,38.6 41.2,40.3 37.2,39.7 C33.6,39.2 31.4,37.4 29.0,37.3 C28.0,37.3 27.8,38.6 28.8,38.9 "
        "C31.0,39.3 33.2,40.9 37.0,41.4 C41.2,41.9 44.4,40.5 48.4,40.9 C49.4,41.0 49.6,39.4 48.6,39.2Z")]))
    els.append(el("scapula_r", "bone", [P(
        "M29.4,38.6 C31.2,38.6 34.6,39.8 36.2,40.8 C36.6,45.6 37.0,51.6 37.4,57.2 C36.2,58.0 34.6,58.2 33.6,57.6 "
        "C31.8,52.6 30.6,47.6 30.2,44.6 C29.4,43.2 28.8,40.4 29.4,38.6Z")]))
    els.append(el("humerus_r", "bone", [C(27.9, 42.6, 2.6), long_bone((26.8, 44.0), ELBOW, 3.4, 1.9, 4.9, 0.12, 0.13)]))
    els.append(el("radius_r", "bone", [long_bone((20.9, 71.6), (12.4, 97.8), 1.5, 1.05, 2.6, 0.12, 0.16)]))
    els.append(el("ulna_r", "bone", [long_bone((23.9, 70.8), (15.9, 98.6), 2.1, 1.0, 1.25, 0.14, 0.1)]))
    els.append(el("hand_r", "bone", hand(WRIST, ELBOW, -1)))
    els.append(el("pelvis_r", "bone", [P(
        "M46.4,93.6 C44.6,89.4 40.0,86.6 35.6,87.4 C33.2,87.9 32.4,90.0 33.4,92.2 C34.6,94.8 37.4,95.6 39.6,96.8 "
        "C40.4,99.6 41.8,102.4 44.4,103.8 C46.0,104.8 48.2,104.6 49.5,103.8 L49.5,100.8 C47.8,100.4 46.8,99.4 46.6,97.8 "
        "C46.4,96.4 46.8,95.0 46.4,93.6Z")],
        details=[{"t": "ellipse", "cx": "45.3", "cy": "100.9", "rx": "1.35", "ry": "1.9", "hole": 1}]))
    els.append(el("femur_r", "bone", [C(42.5, 96.7, 2.1), P(
        "M41.0,97.4 C39.6,96.9 37.8,96.3 36.6,96.9 C35.6,97.5 35.6,100.2 36.9,101.6 L31.2,138.4 C30.0,140.6 28.8,142.0 28.8,144.4 "
        "C28.8,147.0 31.2,147.6 32.6,146.5 C34.0,147.6 36.6,147.2 36.6,144.6 C36.6,142.0 35.5,140.8 34.7,138.6 L40.6,102.6 "
        "C41.6,101.6 42.0,100.0 41.6,99.0Z")]))
    els.append(el("patella_r", "bone", [E(32.7, 142.6, 1.9, 2.4)]))
    els.append(el("tibia_r", "bone", [P(
        "M29.4,148.9 C29.4,148.0 30.2,147.8 31.0,148.0 L35.2,148.0 C36.2,147.8 36.8,148.4 36.6,149.4 C36.4,151.4 35.0,152.8 34.4,154.8 "
        "L33.8,182.8 C34.6,184.0 35.1,185.8 34.5,187.0 C33.7,187.9 32.1,187.6 31.1,187.4 L30.1,187.4 C30.2,185.0 30.6,183.6 31.0,182.6 "
        "L31.4,155.4 C30.4,153.6 29.4,151.6 29.4,148.9Z")]))
    els.append(el("fibula_r", "bone", [P(
        "M27.5,151.0 C27.5,150.0 29.3,149.8 29.5,150.8 C29.7,151.8 29.1,152.8 28.9,153.6 L29.3,184.4 C29.8,185.8 29.8,187.6 28.8,188.8 "
        "C27.8,189.3 27.1,188.3 27.3,186.8 L27.7,153.6 C27.3,152.8 27.5,151.8 27.5,151.0Z")]))
    foot = [P("M28.0,188.9 L35.0,188.9 C35.7,190.0 35.6,191.5 35.0,192.2 L27.4,192.2 C26.9,191.2 27.2,189.7 28.0,188.9Z")]
    for i in range(5):  # 0 = Grosszehe (medial, grösseres x)
        bx = 34.4 - i * 1.65
        ex = 35.0 - i * 2.05
        w = 1.55 if i == 0 else 1.1
        foot.append(capsule((bx, 192.5), (ex, 197.4 - i * 0.35), w))
        foot.append(capsule((ex + (0.1 if i == 0 else -0.05), 198.0 - i * 0.35), (ex + 0.2 - i * 0.25, 200.6 - i * 0.6), w * 0.85))
    els.append(el("foot_r", "bone", foot))
    return els


def ribs_front_right():
    shapes = []
    spec = [(41.0, 7.5, 2.0), (43.2, 10.5, 3.0), (45.4, 12.8, 3.6), (47.6, 14.3, 4.0), (49.8, 15.3, 4.4),
            (52.2, 15.9, 4.6), (54.6, 16.2, 4.8), (57.2, 16.0, 4.6), (59.8, 15.4, 4.2), (62.4, 14.6, 3.8)]
    for i, (y, hw, drop) in enumerate(spec):
        end_x = 48.4 if i < 7 else 48.4 - (i - 6) * 3.0
        end_y = y + drop * 0.55 if i < 7 else y + drop * 0.9
        d = (f"M49.0,{f(y - 2.6)} C{f(50 - hw * 1.2)},{f(y - 3.4)} {f(50 - hw * 1.16)},{f(y + drop)} "
             f"{f(end_x)},{f(end_y)}")
        shapes.append(P(d))
    return shapes


def spine_front():
    els = []
    for i in range(1, 8):
        y = 23.8 + (i - 1) * 2.0
        w = 4.4 + i * 0.12
        els.append(el(f"c{i}", "vertebra", [R(50 - w / 2, y, w, 1.6, 0.55)]))
    for i in range(1, 13):
        y = 38.2 + (i - 1) * 3.15
        w = 4.4 + i * 0.13
        els.append(el(f"t{i}", "vertebra", [R(50 - w / 2, y, w, 2.6, 0.7)]))
    for i in range(1, 6):
        y = 77.4 + (i - 1) * 3.6
        w = 6.0 + i * 0.3
        els.append(el(f"l{i}", "vertebra", [R(50 - w / 2, y, w, 3.0, 0.9)]))
    els.append(el("sacrum", "bone", [P("M45.4,95.4 L54.6,95.4 C54.4,99.4 52.7,104.0 50,106.6 C47.3,104.0 45.6,99.4 45.4,95.4Z")]))
    els.append(el("coccyx", "bone", [P("M49.0,107.0 L51.0,107.0 L50,109.8Z")]))
    return els


def skull_front():
    skull = el("skull", "bone", [P(
        "M41.6,11.0 C41.6,4.4 45.4,1.0 50,1.0 C54.6,1.0 58.4,4.4 58.4,11.0 C58.4,14.4 57.6,16.9 56.6,18.6 L55.8,21.4 "
        "C55.4,22.6 54.2,23.0 53.0,23.0 L47.0,23.0 C45.8,23.0 44.6,22.6 44.2,21.4 L43.4,18.6 C42.4,16.9 41.6,14.4 41.6,11.0Z")],
        details=[E(46.6, 12.4, 2.25, 1.9), E(53.4, 12.4, 2.25, 1.9),
                 P("M49.1,15.0 L50.9,15.0 L50.5,17.6 L49.5,17.6Z"), P("M46.6,20.4 L53.4,20.4")])
    mandible = el("mandible", "bone", [P(
        "M44.4,19.6 C44.6,23.0 46.2,25.8 50,26.0 C53.8,25.8 55.4,23.0 55.6,19.6 L54.3,19.8 C53.9,22.6 52.5,24.4 50,24.5 "
        "C47.5,24.4 46.1,22.6 45.7,19.8Z")])
    return [skull, mandible]


def sternum():
    return el("sternum", "bone", [P("M47.6,40.6 L52.4,40.6 L52.0,44.2 L48.0,44.2Z"),
                                  P("M48.2,44.6 L51.8,44.6 L51.4,60.6 L48.6,60.6Z"), P("M48.9,61.0 L51.1,61.0 L50,63.6Z")])


JOINTS = [("shoulder", SHOULDER, 3.4), ("ac", (29.0, 38.0), 1.7), ("elbow", ELBOW, 2.9), ("wrist", (14.0, 99.4), 2.5),
          ("hip", HIP, 3.2), ("knee", (32.6, 147.2), 3.6), ("ankle", (31.2, 187.6), 2.7)]


def build_front():
    els = []
    els += spine_front()
    right = right_side_front()
    by = {e["id"]: e for e in right}
    order = ["scapula_r", "pelvis_r", "femur_r", "patella_r", "fibula_r", "tibia_r", "foot_r", "humerus_r",
             "ulna_r", "radius_r", "hand_r", "clavicle_r"]
    left = {k.replace("_r", "_l"): mirror_el(v, k.replace("_r", "_l")) for k, v in by.items()}
    # Reihenfolge: hinten liegende zuerst
    seq = []
    for k in order:
        seq += [by[k], left[k.replace("_r", "_l")]]
    els += [seq.pop(0), seq.pop(0)]  # Schulterblätter hinter die Rippen
    ribs_r = el("ribs_r", "rib", ribs_front_right())
    els += [ribs_r, mirror_el(ribs_r, "ribs_l"), sternum()]
    els += seq
    els += skull_front()
    for name, (x, y), r in JOINTS:
        els.append(el(f"joint_{name}_r", "joint", [C(x, y, r)]))
        els.append(el(f"joint_{name}_l", "joint", [C(100 - x, y, r)]))
    els.append(el("joint_sij_r", "joint", [C(46.4, 94.6, 1.8)]))
    els.append(el("joint_sij_l", "joint", [C(53.6, 94.6, 1.8)]))
    # Kiefergelenk: kleiner Punkt am oberen Ende des Unterkiefers (nur vorne sichtbar)
    els.append(el("joint_tmj_r", "joint", [C(44.3, 18.6, 1.3)]))
    els.append(el("joint_tmj_l", "joint", [C(55.7, 18.6, 1.3)]))
    return els


# ------------------------------------------------------------------ Hinten
def build_back():
    front = {e["id"]: e for e in build_front()}
    els = []
    # Wirbel mit Dornfortsätzen
    for i in range(1, 8):
        v = front[f"c{i}"]
        y = float(v["shapes"][0]["y"])
        els.append(el(f"c{i}", "vertebra", v["shapes"] + [R(49.4, y + 0.6, 1.2, 1.8, 0.4)]))
    for i in range(1, 13):
        v = front[f"t{i}"]
        y = float(v["shapes"][0]["y"])
        els.append(el(f"t{i}", "vertebra", v["shapes"] + [R(49.3, y + 1.2, 1.4, 2.6, 0.5)]))
    for i in range(1, 6):
        v = front[f"l{i}"]
        y = float(v["shapes"][0]["y"])
        els.append(el(f"l{i}", "vertebra", v["shapes"] + [R(48.9, y + 0.5, 2.2, 2.4, 0.6)]))
    # Becken hinten: Darmbeinschaufeln
    pel_r = el("pelvis_r", "bone", [P(
        "M53.6,93.4 C55.4,89.0 60.0,86.4 64.4,87.2 C66.8,87.7 67.6,89.8 66.6,92.0 C65.6,94.6 63.0,95.6 61.4,97.0 "
        "C60.6,99.8 59.6,102.6 57.0,104.4 C55.4,105.2 53.4,104.8 52.6,103.6 C53.4,100.4 54.0,97.2 53.6,93.4Z")])
    els += [pel_r, mirror_el(pel_r, "pelvis_l")]
    els.append(el("sacrum", "bone", [P("M45.0,94.4 L55.0,94.4 C54.8,99.0 53.0,104.0 50,107.0 C47.0,104.0 45.2,99.0 45.0,94.4Z")],
                  details=[C(48.4, 97.4, .5), C(51.6, 97.4, .5), C(48.8, 100.2, .45), C(51.2, 100.2, .45), C(49.2, 102.8, .4), C(50.8, 102.8, .4)]))
    els.append(front["coccyx"])
    # Rippen hinten (vom Wirbel nach aussen und unten)
    shapes = []
    spec = [(39.8, 7.0, 2.0), (42.6, 10.4, 3.2), (45.4, 12.8, 3.8), (48.2, 14.4, 4.2), (51.0, 15.4, 4.6),
            (53.8, 15.9, 4.8), (56.6, 16.0, 5.0), (59.4, 15.6, 4.8), (62.2, 14.8, 4.4), (65.0, 13.6, 4.0),
            (67.6, 11.0, 3.0), (69.8, 8.0, 2.0)]
    for y, hw, drop in spec:
        shapes.append(P(f"M52.4,{f(y)} C{f(50 + hw * 0.8)},{f(y - 1.2)} {f(50 + hw * 1.1)},{f(y + drop * 0.5)} "
                        f"{f(50 + hw * 1.04)},{f(y + drop * 1.4)}"))
    ribs_r = el("ribs_r", "rib", shapes)
    els = [ribs_r, mirror_el(ribs_r, "ribs_l")] + els
    # Schulterblatt hinten (rechts = Betrachter rechts)
    scap_r = el("scapula_r", "bone", [P(
        "M57.2,42.4 C60.4,41.4 65.6,41.2 69.0,42.6 C70.0,43.4 69.6,45.4 68.4,46.2 C66.0,50.6 62.6,55.6 59.8,59.0 "
        "C59.0,59.8 58.0,59.4 57.8,58.4 C57.4,53.8 57.0,47.8 56.6,43.8 C56.5,43.0 56.7,42.6 57.2,42.4Z")],
        details=[P("M57.6,46.4 C61.2,45.4 66.0,43.0 71.2,40.6")])
    els += [scap_r, mirror_el(scap_r, "scapula_l")]
    # Gliedmassen: Spiegel der Vorderseite (rechts bleibt rechts, liegt nun beim Betrachter rechts)
    for base in ("clavicle", "humerus", "radius", "ulna", "hand", "femur", "fibula", "tibia"):
        r = front[f"{base}_r"]
        els.append(mirror_el(r, f"{base}_r"))
        els.append(mirror_el(front[f"{base}_l"], f"{base}_l"))
    # Ferse statt Zehen
    heel_r = el("foot_r", "bone", [P("M65.0,188.6 L72.0,188.6 C72.8,190.2 72.4,193.6 70.6,195.4 C69.0,196.6 67.2,196.4 66.0,195.0 "
                                     "C64.6,193.4 64.4,190.2 65.0,188.6Z")])
    els += [heel_r, mirror_el(heel_r, "foot_l")]
    # Schädel hinten
    els.append(el("skull", "bone", [P("M41.6,11.0 C41.6,4.4 45.4,1.0 50,1.0 C54.6,1.0 58.4,4.4 58.4,11.0 C58.4,15.4 57.0,19.2 54.4,21.6 "
                                      "C52.8,22.8 47.2,22.8 45.6,21.6 C43.0,19.2 41.6,15.4 41.6,11.0Z")],
                  details=[P("M44.6,17.4 C47.0,18.6 53.0,18.6 55.4,17.4")]))
    for name, (x, y), r in JOINTS:
        if name == "ac":
            x, y = 29.4, 39.6
        els.append(el(f"joint_{name}_r", "joint", [C(100 - x, y, r)]))
        els.append(el(f"joint_{name}_l", "joint", [C(x, y, r)]))
    els.append(el("joint_sij_r", "joint", [C(54.2, 96.0, 2.0)]))
    els.append(el("joint_sij_l", "joint", [C(45.8, 96.0, 2.0)]))
    return els


data = {"viewBox": "-8 -2 116 206", "front": build_front(), "back": build_back()}
with open(OUT, "w") as fh:
    json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
print("ok", len(data["front"]), len(data["back"]))
