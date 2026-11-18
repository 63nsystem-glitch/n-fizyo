"""Kleine Diagramme als SVG – ohne Bibliothek, für Akte, Verlauf und Patienten-App."""
from __future__ import annotations


def sparkline_svg(points: list[tuple[str, float]], goal: float | None, w: int = 280, h: int = 64) -> str:
    """Kleines Verlaufsdiagramm (SVG) – ohne Bibliothek."""
    values = [v for _, v in points]
    lo = min(values + ([goal] if goal is not None else []))
    hi = max(values + ([goal] if goal is not None else []))
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    pad = 6
    n = len(values)

    def x(i: int) -> float:
        return pad + (w - 2 * pad) * (i / (n - 1) if n > 1 else 0.5)

    def y(v: float) -> float:
        return pad + (h - 2 * pad) * (1 - (v - lo) / (hi - lo))

    d = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    parts = [f'<svg class="sparkline" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true">']
    if goal is not None:
        parts.append(f'<line class="goal" x1="0" x2="{w}" y1="{y(goal):.1f}" y2="{y(goal):.1f}"/>')
    parts.append(f'<path class="line" d="{d}"/>')
    parts += [f'<circle class="dot" cx="{x(i):.1f}" cy="{y(v):.1f}" r="3"/>' for i, v in enumerate(values)]
    parts.append("</svg>")
    return "".join(parts)
