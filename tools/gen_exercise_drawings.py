"""Erzeugt die Übungszeichnungen des Heimprogramms (SVG, eine Datei pro Übung) und die Körperbereich-Kacheln.

Eigene Zeichnung (NSystem), keine fremden Vorlagen. Stil: runde Striche in currentColor (hell/dunkel), eine
Akzentfarbe (--ex-accent) für den bewegten Körperteil und den Bewegungspfeil, die Ausgangsstellung blass.
viewBox 0 0 320 200; die Figur liegt in <g id="fig"> (die App bindet sie mit <use href="...svg#fig"> ein).

Aufruf: python3 tools/gen_exercise_drawings.py [ausgabe_ordner] [code ...]
Standard-Ausgabe: nfizyo/web/static/img/exercises/
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "nfizyo" / "web" / "static" / "img" / "exercises"

# segment lengths at scale 1 (viewBox 0 0 320 200)
T = 50      # hip -> shoulder (neck base)
NECK = 17   # neck base -> head centre
HEAD_R = 11
UA = 32     # upper arm
FA = 29     # forearm incl. hand
TH = 42     # thigh
SH = 40     # shank
FT = 14     # foot
FLOOR = 186
FY = 181    # centre line of a limb resting on the floor

W_TORSO = 13
W_LIMB = 10
W_FOOT = 8
W_PROP = 4
ACCENT = "var(--ex-accent, #D9661F)"
OP_FAR = 0.45
OP_GHOST = 0.2
OP_PROP = 0.45


def at(p, ang, length):
    r = math.radians(ang)
    return (p[0] + length * math.cos(r), p[1] + length * math.sin(r))


def ik(a, c, l1, l2, flip=False):
    """Middle joint between a and c (|ab| = l1, |bc| = l2). flip picks the other side."""
    dx, dy = c[0] - a[0], c[1] - a[1]
    d = math.hypot(dx, dy)
    if d > l1 + l2 - 0.01:
        raise ValueError(f"unreachable: {d:.1f} > {l1 + l2}")
    cos_a = (l1 * l1 + d * d - l2 * l2) / (2 * l1 * d)
    a_ang = math.acos(max(-1.0, min(1.0, cos_a)))
    base = math.atan2(dy, dx)
    ang = base + a_ang if flip else base - a_ang
    return (a[0] + l1 * math.cos(ang), a[1] + l1 * math.sin(ang))


def ang_of(a, b):
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def f(v):
    s = f"{v:.1f}"
    if s == "-0.0":
        s = "0.0"
    return s[:-2] if s.endswith(".0") else s


def d_line(points):
    return "M" + " L".join(f"{f(x)} {f(y)}" for x, y in points)


def d_quad(p0, c, p1):
    return f"M{f(p0[0])} {f(p0[1])} Q{f(c[0])} {f(c[1])} {f(p1[0])} {f(p1[1])}"


def _style_attrs(style):
    """style: 'ink' | 'far' | 'ghost' | 'accent' | 'prop' | float opacity"""
    if style == "accent":
        return [f'style="stroke:{ACCENT}"']
    if style == "far":
        return [f'opacity="{OP_FAR}"']
    if style == "ghost":
        return [f'opacity="{OP_GHOST}"']
    if style == "prop":
        return [f'opacity="{OP_PROP}"']
    if isinstance(style, (int, float)):
        return [f'opacity="{style}"']
    return []


def _fill_attrs(style):
    if style == "accent":
        return [f'stroke="none" style="fill:{ACCENT}"']
    out = ['fill="currentColor" stroke="none"']
    if style == "far":
        out.append(f'opacity="{OP_FAR}"')
    elif style == "ghost":
        out.append(f'opacity="{OP_GHOST}"')
    elif isinstance(style, (int, float)):
        out.append(f'opacity="{style}"')
    return out


class Scene:
    LAYERS = ("props", "ghost", "person", "marks")

    def __init__(self):
        self.layers = {k: [] for k in self.LAYERS}
        self.layer_opacity = {}

    def path(self, layer, d, w, style="ink", extra=""):
        attrs = [f'd="{d}"', f'stroke-width="{f(w)}"'] + _style_attrs(style)
        if extra:
            attrs.append(extra)
        self.layers[layer].append("<path " + " ".join(attrs) + "/>")

    def line(self, layer, points, w, style="ink"):
        self.path(layer, d_line(points), w, style)

    def dot(self, layer, c, r, style="ink"):
        attrs = [f'cx="{f(c[0])}"', f'cy="{f(c[1])}"', f'r="{f(r)}"'] + _fill_attrs(style)
        self.layers[layer].append("<circle " + " ".join(attrs) + "/>")

    def ring(self, layer, c, r, w, style="ink"):
        attrs = [f'cx="{f(c[0])}"', f'cy="{f(c[1])}"', f'r="{f(r)}"', f'stroke-width="{f(w)}"'] + _style_attrs(style)
        self.layers[layer].append("<circle " + " ".join(attrs) + "/>")

    def poly(self, layer, points, style="ink"):
        d = d_line(points) + " Z"
        attrs = [f'd="{d}"'] + _fill_attrs(style)
        self.layers[layer].append("<path " + " ".join(attrs) + "/>")

    def floor(self, x0=20, x1=300, y=FLOOR):
        self.line("props", [(x0, y), (x1, y)], 3, 0.3)

    def prop(self, points, w=W_PROP, style="prop"):
        self.line("props", points, w, style)

    # ---- movement arrows (accent, solid head)
    def arrow(self, points, w=4, head=10, both=False):
        pts = list(points)
        # shorten the shaft so it does not poke through the head
        pts[-1] = _towards(pts[-1], pts[-2], head * 0.55)
        if both:
            pts[0] = _towards(pts[0], pts[1], head * 0.55)
        self.line("marks", pts, w, "accent")
        self._head(points[-2], points[-1], head)
        if both:
            self._head(points[1], points[0], head)

    def curve_arrow(self, p0, c, p1, w=4, head=10, both=False):
        q1 = _towards(p1, c, head * 0.55)
        q0 = _towards(p0, c, head * 0.55) if both else p0
        self.path("marks", d_quad(q0, c, q1), w, "accent")
        self._head(c, p1, head)
        if both:
            self._head(c, p0, head)

    def arc_arrow(self, center, rx, a0, a1, w=4, head=10, ry=None, both=False):
        """Elliptic arc from angle a0 to a1 (degrees, screen coords), head at a1."""
        ry = ry if ry is not None else rx
        n = max(10, int(abs(a1 - a0) / 5))
        pts = []
        for i in range(n + 1):
            a = math.radians(a0 + (a1 - a0) * i / n)
            pts.append((center[0] + rx * math.cos(a), center[1] + ry * math.sin(a)))
        shaft = _trim_end(pts, head * 0.55)
        if both:
            shaft = list(reversed(_trim_end(list(reversed(shaft)), head * 0.55)))
        self.line("marks", shaft, w, "accent")
        self._head(pts[-3], pts[-1], head)
        if both:
            self._head(pts[2], pts[0], head)

    def _head(self, frm, to, size):
        ang = math.atan2(to[1] - frm[1], to[0] - frm[0])
        tip = to
        left = (tip[0] - size * math.cos(ang - 0.48), tip[1] - size * math.sin(ang - 0.48))
        right = (tip[0] - size * math.cos(ang + 0.48), tip[1] - size * math.sin(ang + 0.48))
        d = f"M{f(tip[0])} {f(tip[1])} L{f(left[0])} {f(left[1])} L{f(right[0])} {f(right[1])} Z"
        self.layers["marks"].append(f'<path d="{d}" stroke-width="2.5" style="fill:{ACCENT};stroke:{ACCENT}"/>')

    def svg(self, title):
        body = []
        for name in self.LAYERS:
            items = self.layers[name]
            if items and name in self.layer_opacity:
                items = [f'<g opacity="{self.layer_opacity[name]}">'] + ["  " + x for x in items] + ["</g>"]
            body += items
        inner = "\n    ".join(body)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 200" width="320" height="200">\n'
                f'  <title>{title}</title>\n'
                f'  <g id="fig" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round">\n'
                f'    {inner}\n  </g>\n</svg>\n')


def _towards(p, q, d):
    L = dist(p, q)
    if L < 1e-6:
        return p
    t = min(d / L, 0.9)
    return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)


def _trim_end(pts, d):
    out = list(pts)
    while len(out) > 2 and dist(out[-1], out[-2]) < d:
        d -= dist(out[-1], out[-2])
        out.pop()
    out[-1] = _towards(out[-1], out[-2], d)
    return out


class Person:
    """One figure. Joints: head, sh (neck base), hip, arm origin 'arm' (default sh),
    el_n/wr_n (near arm), el_f/wr_f (far arm), kn_n/an_n/to_n, kn_f/an_f/to_f.
    torso_c: control point for a curved back. nose: face direction in degrees (draws a small nose)."""

    def __init__(self, k=1.0, nose=None, **j):
        self.j = j
        self.k = k
        self.nose = nose

    def draw(self, sc: Scene, styles=None, layer="person", default="ink", far=None):
        """styles: part -> style; parts: arm_f, leg_f, torso, head, leg_n, arm_n. far parts default to 'far'."""
        st = {"arm_f": far or "far", "leg_f": far or "far", "torso": default, "head": default, "leg_n": default,
              "arm_n": default}
        if default == "ghost":
            st = {p: "ghost" for p in st}
        st.update(styles or {})
        j = self.j
        arm0 = j.get("arm", j.get("sh"))
        arm0f = j.get("arm_f0", arm0)
        if "el_f" in j:
            sc.line(layer, [arm0f, j["el_f"], j["wr_f"]], W_LIMB, st["arm_f"])
        if "kn_f" in j:
            sc.line(layer, [j["hip"], j["kn_f"], j["an_f"]], W_LIMB, st["leg_f"])
            if "to_f" in j:
                sc.line(layer, [j["an_f"], j["to_f"]], W_FOOT, st["leg_f"])
        if "sh" in j and "hip" in j:
            if "torso_c" in j:
                sc.path(layer, d_quad(j["hip"], j["torso_c"], j["sh"]), W_TORSO, st["torso"])
            else:
                sc.line(layer, [j["hip"], j["sh"]], W_TORSO, st["torso"])
        if "head" in j:
            sc.line(layer, [j["sh"], j["head"]], W_LIMB, st["head"])
            r = HEAD_R * self.k
            sc.dot(layer, j["head"], r, st["head"])
            if self.nose is not None:
                c = j["head"]
                a = self.nose
                p1, p2 = at(c, a - 30, r - 0.8), at(c, a + 24, r - 0.8)
                tip = at(c, a + 12, r + 4.5 * self.k)
                sc.poly(layer, [p1, tip, p2], st["head"])
        if "kn_n" in j:
            sc.line(layer, [j["hip"], j["kn_n"], j["an_n"]], W_LIMB, st["leg_n"])
            if "to_n" in j:
                sc.line(layer, [j["an_n"], j["to_n"]], W_FOOT, st["leg_n"])
        if "el_n" in j:
            sc.line(layer, [arm0, j["el_n"], j["wr_n"]], W_LIMB, st["arm_n"])


def clamp_reach(a, target, l1, l2, frac=0.97):
    """target, or the nearest point towards it that a limb of l1 + l2 can reach."""
    d = dist(a, target)
    m = (l1 + l2) * frac
    if d <= m:
        return target
    return (a[0] + (target[0] - a[0]) * m / d, a[1] + (target[1] - a[1]) * m / d)


# ------------------------------------------------------------------ building blocks


def supine(hip, k):
    """Lying on the back, head to the left, face up. Returns joints without legs."""
    sh = (hip[0] - T * k, hip[1])
    r = HEAD_R * k
    hy = FLOOR - 1.5 - r
    dy = sh[1] - hy
    dx = ((NECK * k) ** 2 - dy ** 2) ** 0.5
    head = (sh[0] - dx, hy)
    return dict(hip=hip, sh=sh, head=head)


def leg_bent_to(hip, an, k, up=True):
    """Knee for a leg from hip to ankle; up=True puts the knee above the line."""
    a = ik(hip, an, TH * k, SH * k, flip=False)
    b = ik(hip, an, TH * k, SH * k, flip=True)
    return a if (a[1] < b[1]) == up else b


def stool(sc, x0, x1, top):
    sc.prop([(x0, top), (x1, top)], w=5)
    sc.prop([(x0 + 6, top), (x0 + 2, FLOOR)])
    sc.prop([(x1 - 6, top), (x1 - 2, FLOOR)])


def chair_back(sc, x, top=92, seat=140, depth=44, side=1):
    """Chair seen from the side; the back post at x, the seat towards side (+1 right, -1 left)."""
    sc.prop([(x, FLOOR), (x, top)])
    sc.prop([(x, seat), (x + side * depth, seat), (x + side * depth, FLOOR)])


# ------------------------------------------------------------------ knee
def quad_set():
    sc = Scene()
    sc.floor()
    k = 1.25
    hip = (92, FY - 1)
    sh = at(hip, -101, T * k)
    head = at(sh, -97, NECK * k)
    wr = (hip[0] - 36, FY)
    el = ik(sh, wr, UA * k, FA * k, flip=True)
    kn = (hip[0] + TH * k, FY - 1)
    an = (kn[0] + SH * k, FY - 1)
    to = at(an, -78, FT * k)
    # start: knee a little bent (ghost)
    gan = (an[0] - 7, FY - 1)
    gkn = leg_bent_to(hip, gan, k)
    Person(k, hip=hip, kn_n=gkn, an_n=gan, to_n=at(gan, -78, FT * k)).draw(sc, layer="ghost", default="ghost")
    Person(k, hip=hip, sh=sh, head=head, el_n=el, wr_n=wr, kn_n=kn, an_n=an, to_n=to).draw(sc, {"leg_n": "accent"})
    sc.arrow([(kn[0] + 4, kn[1] - 58), (kn[0] + 4, kn[1] - 14)])
    return sc


def heel_slide():
    sc = Scene()
    sc.floor()
    k = 1.25
    hip = (138, FY - 1)
    j = supine(hip, k)
    kf = (hip[0] + TH * k, FY)
    af = (kf[0] + SH * k, FY)
    tf = at(af, -80, FT * k)
    an = (hip[0] + 64, FY)
    kn = leg_bent_to(hip, an, k)
    to = (an[0] + FT * k, FY + 1)
    Person(k, **j, kn_f=kf, an_f=af, to_f=tf, kn_n=kn, an_n=an, to_n=to).draw(sc, {"leg_n": "accent"})
    y = FY - 18
    sc.arrow([(af[0] + 2, y), (an[0] + 26, y)])
    return sc


def mini_squat():
    sc = Scene()
    sc.floor()
    cx = 194
    chair_back(sc, cx)
    an = (150, FY - 1)
    to = (an[0] + FT, FY + 1)
    # start: upright (ghost)
    ghip = (an[0], an[1] - TH - SH)
    gsh = at(ghip, -90, T)
    Person(1, hip=ghip, sh=gsh, head=at(gsh, -90, NECK), kn_n=(an[0], an[1] - SH), an_n=an, to_n=to).draw(
        sc, layer="ghost", default="ghost")
    hip = (an[0] - 16, an[1] - 66)
    kn = ik(hip, an, TH, SH, flip=False)
    sh = at(hip, -76, T)
    head = at(sh, -74, NECK)
    wr = (cx - 6, 102)
    el = ik(sh, wr, UA, FA, flip=True)
    Person(1, hip=hip, sh=sh, head=head, kn_n=kn, an_n=an, to_n=to, el_n=el, wr_n=wr).draw(sc, {"leg_n": "accent"})
    sc.arrow([(hip[0] - 30, hip[1] - 34), (hip[0] - 30, hip[1] - 2)])
    return sc


def slr():
    sc = Scene()
    sc.floor()
    k = 1.25
    hip = (128, FY - 1)
    j = supine(hip, k)
    af = (hip[0] + 66, FY)
    kf = leg_bent_to(hip, af, k)
    tf = (af[0] + FT * k, FY + 1)
    gk = (hip[0] + TH * k, FY)
    ga = (gk[0] + SH * k, FY)
    Person(k, hip=hip, kn_n=gk, an_n=ga, to_n=at(ga, -80, FT * k)).draw(sc, layer="ghost", default="ghost")
    kn = at(hip, -24, TH * k)
    an = at(kn, -24, SH * k)
    to = at(an, -24 - 78, FT * k)
    Person(k, **j, kn_f=kf, an_f=af, to_f=tf, kn_n=kn, an_n=an, to_n=to).draw(sc, {"leg_n": "accent"})
    sc.arc_arrow(hip, (TH + SH) * k + 16, -4, -22)
    return sc


def calf_raise():
    sc = Scene()
    sc.floor()
    k = 0.96
    cx = 205
    chair_back(sc, cx, top=90, seat=138)
    ball = (184, FY + 1)
    ft = FT * k * 1.35
    an = at(ball, -126, ft)
    # start: heels down (ghost leg)
    gan = (ball[0] - ft, FY - 1)
    gkn = (gan[0] - 1, gan[1] - SH * k)
    sc.line("ghost", [gkn, gan, (ball[0] + 1, FY + 1)], W_LIMB, "ghost")
    kn = (an[0] - 1, an[1] - SH * k)
    hip = (kn[0] - 1, kn[1] - TH * k)
    sh = at(hip, -90, T * k)
    head = at(sh, -90, NECK * k)
    wr = clamp_reach(sh, (cx - 6, 95), UA * k, FA * k)
    el = ik(sh, wr, UA * k, FA * k, flip=True)
    Person(k, hip=hip, sh=sh, head=head, kn_n=kn, an_n=an, to_n=ball, el_n=el, wr_n=wr).draw(sc, {"leg_n": "accent"})
    x = gan[0] - 16
    sc.arrow([(x, FY - 2), (x, an[1] - 22)])
    return sc


def single_leg():
    sc = Scene()
    sc.floor()
    cx = 200
    chair_back(sc, cx, top=92, seat=140)
    af = (150, FY - 1)
    tf = (af[0] + FT, FY + 1)
    hip = (153, af[1] - 80)
    kf = leg_bent_to(hip, af, 1, up=False)
    kf = ik(hip, af, TH, SH, flip=True)
    sh = at(hip, -90, T)
    head = at(sh, -90, NECK)
    kn = at(hip, 97, TH)
    an = at(kn, 186, SH)
    to = at(an, 100, FT * 0.9)
    wr = clamp_reach(sh, (cx - 7, 96), UA, FA)
    el = ik(sh, wr, UA, FA, flip=True)
    p = Person(1, hip=hip, sh=sh, head=head, kn_f=kf, an_f=af, to_f=tf, kn_n=kn, an_n=an, to_n=to,
               el_n=el, wr_n=wr)
    p.draw(sc, {"leg_n": "accent", "leg_f": "ink"})
    sc.arrow([(an[0] + 4, FY - 4), (an[0] + 4, an[1] + 18)])
    return sc


# ------------------------------------------------------------------ shoulder
def pendulum():
    sc = Scene()
    sc.floor()
    top = 122
    sc.prop([(204, top), (300, top)], w=5)
    sc.prop([(212, top), (212, FLOOR)])
    sc.prop([(292, top), (292, FLOOR)])
    hip = (116, 104)
    an_n, an_f = (100, FY - 1), (132, FY - 1)
    kn_n = ik(hip, an_n, TH, SH, flip=False)
    kn_f = ik(hip, an_f, TH, SH, flip=False)
    sh = at(hip, -10, T)
    head = at(sh, 0, NECK)
    wr_f = (214, top - 6)
    el_f = ik(sh, wr_f, UA, FA, flip=True)
    el_n = at(sh, 90, UA)
    wr_n = at(el_n, 90, FA)
    Person(1, hip=hip, sh=sh, head=head, kn_n=kn_n, an_n=an_n, to_n=(an_n[0] + FT, FY + 1), kn_f=kn_f, an_f=an_f,
           to_f=(an_f[0] + FT, FY + 1), el_f=el_f, wr_f=wr_f, el_n=el_n, wr_n=wr_n).draw(sc, {"arm_n": "accent"})
    sc.arc_arrow((wr_n[0], wr_n[1] + 6), 22, 160, 500, ry=8)
    return sc


def _front_torso(sc, cx, sh_y, hip_y, half_sh=24, half_hip=17, style="ink"):
    """Torso seen from the front or back: rounded outline."""
    d = (f"M{cx - half_sh} {sh_y + 6} Q{cx - half_sh} {sh_y - 2} {cx - half_sh + 10} {sh_y - 3} L{cx + half_sh - 10} {sh_y - 3}"
         f" Q{cx + half_sh} {sh_y - 2} {cx + half_sh} {sh_y + 6} L{cx + half_hip} {hip_y} L{cx - half_hip} {hip_y} Z")
    sc.path("person", d, W_LIMB, style)


def scap_retraction():
    """Seen from behind, sitting on a stool."""
    sc = Scene()
    sc.floor()
    cx = 160
    sh_y, hip_y = 60, 142
    stool(sc, 114, 206, 152)
    # lower legs from behind
    for x in (cx - 15, cx + 15):
        sc.line("person", [(x, hip_y + 8), (x, FY)], W_LIMB, "far")
    # arms hanging at the sides
    for s in (-1, 1):
        sc.line("person", [(cx + s * 30, sh_y + 4), (cx + s * 38, sh_y + 42), (cx + s * 33, sh_y + 78)], W_LIMB)
    _front_torso(sc, cx, sh_y, hip_y, half_sh=27, half_hip=18)
    sc.line("person", [(cx, sh_y - 4), (cx, sh_y - 16)], W_LIMB)
    sc.dot("person", (cx, sh_y - 30), 14)
    # spine
    sc.line("person", [(cx, sh_y + 4), (cx, hip_y - 6)], 3, 0.35)
    for s in (-1, 1):
        def blade(dx):
            x0 = cx + s * (6 + dx)
            return [(x0, sh_y + 8), (x0 + s * 15, sh_y + 6), (x0 + s * 2, sh_y + 40)]
        sc.poly("ghost", blade(9), "ghost")
        sc.poly("person", blade(0), "accent")
        sc.arrow([(cx + s * 24, sh_y + 54), (cx + s * 7, sh_y + 54)], w=3.5, head=8)
    return sc


def er_band():
    """From the front: elbows at the sides, forearms turn outwards against a band."""
    sc = Scene()
    sc.floor()
    cx = 160
    sh_y, hip_y = 56, 116
    for s in (-1, 1):
        sc.line("person", [(cx + s * 10, hip_y - 2), (cx + s * 12, hip_y + 32), (cx + s * 13, FY)], W_LIMB)
        sc.line("person", [(cx + s * 13, FY), (cx + s * 24, FY + 1)], W_FOOT)
    _front_torso(sc, cx, sh_y, hip_y, half_sh=22, half_hip=15)
    sc.line("person", [(cx, sh_y - 4), (cx, sh_y - 14)], W_LIMB)
    sc.dot("person", (cx, sh_y - 27), 13)
    ew = 28
    ey = sh_y + 40
    for s in (-1, 1):
        el = (cx + s * ew, ey)
        sc.line("person", [(cx + s * 29, sh_y + 3), el], W_LIMB)
        # start: forearm points forwards, hand in front of the belly (ghost)
        sc.line("ghost", [el, (cx + s * 13, ey + 6)], W_LIMB, "ghost")
        sc.line("person", [el, (cx + s * (ew + 30), ey - 2)], W_LIMB, "accent")
        sc.curve_arrow((cx + s * 16, ey + 18), (cx + s * 38, ey + 30), (cx + s * 58, ey + 14), head=9)
    sc.line("ghost", [(cx - 13, ey + 6), (cx + 13, ey + 6)], 3, "ghost")
    sc.path("person", f"M{cx - ew - 30} {ey - 2} Q{cx} {ey + 3} {cx + ew + 30} {ey - 2}", 2.5, 0.6)
    return sc


def wall_slide():
    sc = Scene()
    sc.floor()
    k = 0.92
    an = (150, FY - 1)
    to = (an[0] + FT * k, FY + 1)
    kn = (an[0] + 1, an[1] - SH * k)
    hip = (an[0] + 3, kn[1] - TH * k)
    sh = at(hip, -84, T * k)
    head = at(sh, -82, NECK * k)
    ua, fa = UA * k, FA * k
    dy0 = 12
    ex0 = sh[0] + (ua ** 2 - dy0 ** 2) ** 0.5
    wall = ex0 + 8
    sc.prop([(wall, FLOOR), (wall, 8)], w=5)
    # start (ghost): elbows below shoulder height, forearms up the wall
    gel = (ex0, sh[1] + dy0)
    sc.line("ghost", [sh, gel, (gel[0], gel[1] - fa)], W_LIMB, "ghost")
    dy1 = -16
    el = (sh[0] + (ua ** 2 - dy1 ** 2) ** 0.5, sh[1] + dy1)
    wr = (ex0, el[1] - fa)
    Person(k, hip=hip, sh=sh, head=head, kn_n=kn, an_n=an, to_n=to, el_n=el, wr_n=wr).draw(sc, {"arm_n": "accent"})
    x = wall + 20
    sc.arrow([(x, gel[1] - 4), (x, wr[1] + 4)])
    return sc


# ------------------------------------------------------------------ lumbar / hip
def pelvic_tilt():
    sc = Scene()
    sc.floor()
    k = 1.25
    hip = (158, FY - 1)
    j = supine(hip, k)
    an = (hip[0] + 72, FY)
    kn = leg_bent_to(hip, an, k)
    to = (an[0] + FT * k, FY + 1)
    sh = j["sh"]
    mid = ((hip[0] + sh[0]) / 2 + 8, hip[1])
    sc.path("ghost", f"M{hip[0]} {hip[1]} Q{mid[0]} {hip[1] - 26} {sh[0]} {sh[1]}", W_TORSO, "ghost")
    Person(k, **j, kn_n=kn, an_n=an, to_n=to).draw(sc, {"torso": "accent"})
    sc.arrow([(mid[0], hip[1] - 52), (mid[0], hip[1] - 14)])
    return sc


def _quadruped(hip_x, k, arm_k=0.86):
    hip = (hip_x, FY - TH * k)
    kn = (hip_x, FY)
    an = (hip_x - SH * k, FY)
    to = (an[0] - FT * k * 0.9, FY + 1)
    arm = (UA + FA) * k * arm_k
    sh_y = FY - arm
    dx = ((T * k) ** 2 - (hip[1] - sh_y) ** 2) ** 0.5
    sh = (hip_x + dx, sh_y)
    return hip, kn, an, to, sh


def cat_cow():
    sc = Scene()
    sc.floor()
    k = 1.15
    hip, kn, an, to, sh = _quadruped(116, k)
    wr = (sh[0] + 2, FY)
    el = ((sh[0] + wr[0]) / 2, (sh[1] + wr[1]) / 2)
    mid = ((hip[0] + sh[0]) / 2, (hip[1] + sh[1]) / 2)
    # cow (ghost): back sags, head up
    gh = at(sh, -40, NECK * k)
    sc.path("ghost", f"M{hip[0]:.1f} {hip[1]:.1f} Q{mid[0]:.1f} {mid[1] + 30:.1f} {sh[0]:.1f} {sh[1]:.1f}", W_TORSO, "ghost")
    Person(k, sh=sh, head=gh).draw(sc, layer="ghost", default="ghost")
    # cat: back rounded, head down
    head = at(sh, 68, NECK * k)
    Person(k, hip=hip, sh=sh, head=head, torso_c=(mid[0], mid[1] - 40), kn_n=kn, an_n=an, to_n=to,
           el_n=el, wr_n=wr).draw(sc, {"torso": "accent", "head": "accent"})
    x = mid[0]
    top = mid[1] - 20 - 10
    sc.arrow([(x, top), (x, top - 30)], both=True)
    return sc


def bird_dog():
    sc = Scene()
    sc.floor()
    k = 1.0
    hip, kn, an, to, sh = _quadruped(122, k, arm_k=0.88)
    wr_f = (sh[0] + 2, FY)
    el_f = ((sh[0] + wr_f[0]) / 2, (sh[1] + wr_f[1]) / 2)
    arm0 = (sh[0] - 1, sh[1] + 7)
    el_n = at(arm0, -5, UA * k)
    wr_n = at(el_n, -5, FA * k)
    head = at(sh, -42, NECK * k + 1)
    kn_f = at(hip, 182, TH * k)
    an_f = at(kn_f, 182, SH * k)
    to_f = at(an_f, 96, FT * k * 0.85)
    Person(k, hip=hip, sh=sh, head=head, arm=arm0, arm_f0=arm0, kn_n=kn, an_n=an, to_n=to, el_f=el_f, wr_f=wr_f,
           el_n=el_n, wr_n=wr_n, kn_f=kn_f, an_f=an_f, to_f=to_f).draw(sc, {"arm_n": "accent", "leg_f": "accent"})
    y1 = wr_n[1] - 22
    sc.arrow([(wr_n[0] - 30, y1), (wr_n[0] + 6, y1)])
    y2 = an_f[1] - 22
    sc.arrow([(an_f[0] + 30, y2), (an_f[0] - 6, y2)])
    return sc


def bridge():
    sc = Scene()
    sc.floor()
    k = 1.2
    sh = (92, FY - 1)
    a = -24
    hip = at(sh, a, T * k)
    kn = at(hip, a, TH * k)
    dy = FY - kn[1]
    an = (kn[0] + ((SH * k) ** 2 - dy ** 2) ** 0.5, FY)
    to = (an[0] + FT * k, FY + 1)
    r = HEAD_R * k
    hy = FLOOR - 1.5 - r
    head = (sh[0] - ((NECK * k) ** 2 - (sh[1] - hy) ** 2) ** 0.5, hy)
    el = (sh[0] + UA * k, FY)
    wr = (el[0] + FA * k, FY)
    # start: pelvis on the floor (ghost)
    ghip = (sh[0] + T * k, FY - 1)
    gkn = leg_bent_to(ghip, an, k)
    Person(k, hip=ghip, sh=sh, kn_n=gkn, an_n=an).draw(sc, layer="ghost", default="ghost")
    Person(k, hip=hip, sh=sh, head=head, el_n=el, wr_n=wr, kn_n=kn, an_n=an, to_n=to).draw(sc, {"torso": "accent"})
    sc.arrow([(hip[0] + 2, hip[1] - 14), (hip[0] + 2, hip[1] - 48)])
    return sc


def side_leg_raise():
    """Lying on the side, seen from the front; head to the left, propped on the lower forearm."""
    sc = Scene()
    sc.floor()
    k = 1.15
    sh = (98, 150)
    hip_y = FY - 9
    dy = hip_y - sh[1]
    hip = (sh[0] + ((T * k) ** 2 - dy ** 2) ** 0.5, hip_y)
    el_f = (sh[0] - 8, FY)
    head = at(sh, 205, NECK * k)
    wr_f = (head[0] - 8, head[1] + 12)
    # lower leg on the floor
    kn_f = (hip[0] + TH * k, FY - 2)
    an_f = (kn_f[0] + SH * k, FY)
    to_f = at(an_f, 80, FT * k * 0.5)
    # start (ghost): upper leg resting on the lower one
    gk = (hip[0] + TH * k, hip_y - 1)
    ga = (gk[0] + SH * k, hip_y + 1)
    sc.line("ghost", [hip, gk, ga], W_LIMB, "ghost")
    kn = at(hip, -22, TH * k)
    an = at(kn, -22, SH * k)
    to = at(an, -22 + 80, FT * k * 0.5)
    Person(k, hip=hip, sh=sh, head=head, el_f=el_f, wr_f=wr_f, kn_f=kn_f, an_f=an_f, to_f=to_f,
           kn_n=kn, an_n=an, to_n=to).draw(sc, {"leg_n": "accent", "arm_f": "ink", "leg_f": "ink"})
    sc.arc_arrow(hip, (TH + SH) * k + 14, -6, -24)
    return sc


def walk():
    sc = Scene()
    sc.floor()
    hip = (140, 107)
    an_n = (174, FY - 2)
    kn_n = ik(hip, an_n, TH, SH, flip=False)
    to_n = at(an_n, -30, FT)
    to_f = (118, FY + 1)
    an_f = at(to_f, -146, FT)
    kn_f = ik(hip, an_f, TH, SH, flip=False)
    sh = at(hip, -86, T)
    head = at(sh, -84, NECK)
    el_n = at(sh, 114, UA)
    wr_n = at(el_n, 100, FA)
    el_f = at(sh, 66, UA)
    wr_f = at(el_f, 30, FA)
    Person(1, hip=hip, sh=sh, head=head, kn_n=kn_n, an_n=an_n, to_n=to_n, kn_f=kn_f, an_f=an_f, to_f=to_f,
           el_n=el_n, wr_n=wr_n, el_f=el_f, wr_f=wr_f).draw(sc)
    sc.arrow([(212, 120), (266, 120)])
    return sc


# ------------------------------------------------------------------ neck / sitting
def seated(sc, hip, k):
    kn = (hip[0] + TH * k, hip[1])
    an = (kn[0], FY)
    to = (an[0] + FT * k, FY + 1)
    sh = at(hip, -90, T * k)
    stool(sc, hip[0] - 24 * k, hip[0] + 26 * k, hip[1] + 8)
    return dict(hip=hip, kn_n=kn, an_n=an, to_n=to, sh=sh)


def chin_tuck():
    sc = Scene()
    sc.floor()
    k = 1.3
    hip = (118, FY - SH * k)
    j = seated(sc, hip, k)
    sh = j["sh"]
    el = at(sh, 86, UA * k)
    wr = at(el, 22, FA * k)
    head = at(sh, -99, NECK * k)
    ghead = at(sh, -64, NECK * k + 2)
    Person(k, sh=sh, head=ghead).draw(sc, layer="ghost", default="ghost")
    Person(k, **j, head=head, el_n=el, wr_n=wr).draw(sc, {"head": "accent"})
    yy = head[1] - HEAD_R * k - 14
    sc.arrow([(ghead[0] + 22, yy), (head[0] - 4, yy)])
    return sc


def shoulder_circles():
    sc = Scene()
    sc.floor()
    k = 1.3
    hip = (126, FY - SH * k)
    j = seated(sc, hip, k)
    sh = j["sh"]
    arm0 = (sh[0], sh[1] + 13)
    el = at(arm0, 88, UA * k)
    wr = at(el, 22, FA * k)
    head = at(sh, -90, NECK * k)
    Person(k, **j, head=head, arm=arm0, el_n=el, wr_n=wr).draw(sc)
    sc.dot("person", arm0, 7.5, "accent")
    # backwards circle (figure faces right): up in front, back over the top, down behind
    sc.arc_arrow(arm0, 19, 40, -275, ry=15)
    return sc


def ankle_abc():
    sc = Scene()
    sc.floor()
    k = 1.1
    hip = (92, FY - SH * k)
    top = hip[1] + 8
    sc.prop([(hip[0] - 24, top), (hip[0] + 30, top)], w=5)
    sc.prop([(hip[0] - 20, top), (hip[0] - 22, FLOOR)])
    sc.prop([(hip[0] + 26, top), (hip[0] + 28, FLOOR)])
    sc.prop([(hip[0] - 20, top), (hip[0] - 26, top - 70)])
    kf = (hip[0] + TH * k, hip[1])
    af = (kf[0], FY)
    tf = (af[0] + FT * k, FY + 1)
    sh = at(hip, -93, T * k)
    head = at(sh, -90, NECK * k)
    kn = at(hip, -6, TH * k)
    an = at(kn, 4, SH * k)
    to = at(an, -70, FT * k)
    wr = (hip[0] + 30, hip[1] - 9)
    el = ik(sh, wr, UA * k, FA * k, flip=True)
    Person(k, hip=hip, sh=sh, head=head, kn_f=kf, an_f=af, to_f=tf, kn_n=kn, an_n=an, to_n=to,
           el_n=el, wr_n=wr).draw(sc, {"leg_n": "accent"})
    # letters A B C written in the air by the foot
    x, y = to[0] + 22, to[1] - 14
    w = 4.5
    sc.path("marks", f"M{x:.1f} {y + 26:.1f} L{x + 10:.1f} {y:.1f} L{x + 20:.1f} {y + 26:.1f} M{x + 4.5:.1f} {y + 15:.1f} L{x + 15.5:.1f} {y + 15:.1f}",
            w, "accent")
    bx = x + 32
    sc.path("marks", f"M{bx:.1f} {y + 26:.1f} L{bx:.1f} {y:.1f} L{bx + 8:.1f} {y:.1f} Q{bx + 16:.1f} {y:.1f} {bx + 16:.1f} {y + 6.5:.1f}"
                     f" Q{bx + 16:.1f} {y + 12.5:.1f} {bx + 8:.1f} {y + 12.5:.1f} L{bx:.1f} {y + 12.5:.1f} M{bx + 8:.1f} {y + 12.5:.1f}"
                     f" Q{bx + 18:.1f} {y + 12.5:.1f} {bx + 18:.1f} {y + 19.3:.1f} Q{bx + 18:.1f} {y + 26:.1f} {bx + 9:.1f} {y + 26:.1f} L{bx:.1f} {y + 26:.1f}",
            w, "accent")
    c = bx + 32
    sc.path("marks", f"M{c + 17:.1f} {y + 4:.1f} Q{c + 12:.1f} {y - 1:.1f} {c + 7:.1f} {y:.1f} Q{c:.1f} {y + 2:.1f} {c:.1f} {y + 13:.1f}"
                     f" Q{c:.1f} {y + 24:.1f} {c + 7:.1f} {y + 26:.1f} Q{c + 13:.1f} {y + 27:.1f} {c + 17:.1f} {y + 21:.1f}", w, "accent")
    return sc


FIGURES = {
    "quad_set": (quad_set, "Quadrizeps anspannen"),
    "heel_slide": (heel_slide, "Fersenrutschen"),
    "mini_squat": (mini_squat, "Mini-Kniebeuge"),
    "slr": (slr, "Gestrecktes Bein heben"),
    "calf_raise": (calf_raise, "Fersen heben"),
    "pendulum": (pendulum, "Pendelübung"),
    "scap_retraction": (scap_retraction, "Schulterblätter zusammenziehen"),
    "er_band": (er_band, "Aussenrotation mit Band"),
    "wall_slide": (wall_slide, "Wandrutschen"),
    "pelvic_tilt": (pelvic_tilt, "Beckenkippen"),
    "cat_cow": (cat_cow, "Katze und Kuh"),
    "bridge": (bridge, "Brücke"),
    "bird_dog": (bird_dog, "Vierfüssler diagonal"),
    "chin_tuck": (chin_tuck, "Kinn zurückziehen"),
    "shoulder_circles": (shoulder_circles, "Schulterkreisen"),
    "single_leg": (single_leg, "Einbeinstand"),
    "ankle_abc": (ankle_abc, "Fuss-ABC"),
    "side_leg_raise": (side_leg_raise, "Bein seitlich heben"),
    "walk": (walk, "Spazieren gehen"),
}

# ------------------------------------------------------------------ body-region tiles (fallback without a drawing)
def region(name):
    """Neutral front view (no exercise pose) with the body region marked."""
    sc = Scene()
    cx = 160
    full = name == "general"
    st = "accent" if full else "ink"
    if not full:
        sc.layer_opacity["person"] = 0.5
    sc.dot("person", (cx, 28), 12, st)
    sc.line("person", [(cx, 40), (cx, 50)], W_LIMB, st)
    d = (f"M{cx - 24} {58} Q{cx - 24} {50} {cx - 14} {49} L{cx + 14} {49} Q{cx + 24} {50} {cx + 24} {58}"
         f" L{cx + 17} {112} L{cx - 17} {112} Z")
    sc.path("person", d, W_LIMB, st)
    for s_ in (-1, 1):
        sc.line("person", [(cx + s_ * 29, 56), (cx + s_ * 35, 90), (cx + s_ * 37, 122)], W_LIMB, st)
        sc.line("person", [(cx + s_ * 9, 116), (cx + s_ * 11, 149), (cx + s_ * 12, 180)], W_LIMB, st)
        sc.line("person", [(cx + s_ * 12, 180), (cx + s_ * 21, 182)], W_FOOT, st)
    spots = {"knee": [(cx - 11, 149), (cx + 11, 149)], "shoulder": [(cx - 27, 55), (cx + 27, 55)],
             "hip": [(cx - 12, 110), (cx + 12, 110)], "ankle": [(cx - 13, 179), (cx + 13, 179)]}
    for p in spots.get(name, []):
        sc.dot("marks", p, 9.5, "accent")
    if name == "cervical":
        sc.line("marks", [(cx, 38), (cx, 54)], 11, "accent")
    if name == "lumbar":
        sc.line("marks", [(cx, 84), (cx, 106)], 11, "accent")
    return sc


REGIONS = {"knee": "Knie", "shoulder": "Schulter", "hip": "Hüfte", "lumbar": "Lendenwirbelsäule", "cervical": "Nacken",
           "ankle": "Sprunggelenk / Fuss", "general": "Ganzer Körper"}

if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else OUT)
    out.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[2:])
    for code, (fn, title) in FIGURES.items():
        if only and code not in only:
            continue
        (out / f"{code}.svg").write_text(fn().svg(title), encoding="utf-8")
    for name, title in REGIONS.items():
        if only and f"region-{name}" not in only:
            continue
        (out / f"region-{name}.svg").write_text(region(name).svg(title), encoding="utf-8")
    print("ok", len(FIGURES), len(REGIONS))

