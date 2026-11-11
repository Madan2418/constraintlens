"""Dependency-free SVG figures for the evaluation report (ARCHITECTURE.md §14.4 step 7).

Each figure is one horizontal bar chart: one metric, one series, one bar per
configuration, with its 95% bootstrap interval as a whisker. A single series needs no
legend: the title names it. Values are labeled at the bar end, and every bar carries a
``<title>`` tooltip. Colors are CSS custom properties with a dark-mode override, so the
files render on either GitHub theme.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

ROW_H = 26
BAR_H = 14
LABEL_W = 290
PLOT_W = 380
PAD = 16
TITLE_H = 44

_STYLE = """
<style>
  .viz { --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e; --grid:#e4e3df; --bar:#2a78d6; }
  @media (prefers-color-scheme: dark) {
    .viz { --surface:#1a1a19; --ink:#f0efec; --ink-2:#c3c2b7; --grid:#3a3a37; --bar:#3987e5; }
  }
  .bg { fill: var(--surface); }
  .title { fill: var(--ink); font: 600 14px system-ui, sans-serif; }
  .sub { fill: var(--ink-2); font: 12px system-ui, sans-serif; }
  .lab { fill: var(--ink); font: 12px system-ui, sans-serif; }
  .val { fill: var(--ink-2); font: 11px ui-monospace, monospace; }
  .grid { stroke: var(--grid); stroke-width: 1; }
  .bar { fill: var(--bar); }
  .ci { stroke: var(--ink-2); stroke-width: 1.5; }
</style>
"""


def bar_chart(
    title: str,
    subtitle: str,
    rows: Sequence[tuple[str, float | None, tuple[float, float] | None]],
) -> str:
    """``rows``: (label, value in [0, 1] or None for "not run", 95% CI or None)."""
    height = TITLE_H + PAD + ROW_H * len(rows) + 24
    width = LABEL_W + PLOT_W + 70
    x0 = LABEL_W

    def x(v: float) -> float:
        return x0 + PLOT_W * max(0.0, min(1.0, v))

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" class="viz" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-label="{escape(title)}">',
        _STYLE,
        f'<rect class="bg" width="{width}" height="{height}" rx="8"/>',
        f'<text class="title" x="{PAD}" y="22">{escape(title)}</text>',
        f'<text class="sub" x="{PAD}" y="38">{escape(subtitle)}</text>',
    ]
    top = TITLE_H + PAD
    bottom = top + ROW_H * len(rows)
    for tick in (0.0, 0.25, 0.5, 0.75, 1.0):
        tx = x(tick)
        out.append(f'<line class="grid" x1="{tx:.1f}" y1="{top - 6}" x2="{tx:.1f}" y2="{bottom}"/>')
        out.append(
            f'<text class="val" x="{tx:.1f}" y="{bottom + 16}" text-anchor="middle">'
            f"{int(tick * 100)}%</text>"
        )
    for i, (label, value, ci) in enumerate(rows):
        cy = top + i * ROW_H + ROW_H / 2
        out.append(
            f'<text class="lab" x="{x0 - 10}" y="{cy + 4:.1f}" text-anchor="end">'
            f"{escape(label)}</text>"
        )
        if value is None:
            out.append(f'<text class="val" x="{x0 + 4}" y="{cy + 4:.1f}">not run</text>')
            continue
        w = max(0.0, x(value) - x0)
        tip = f"{label}: {value:.1%}" + (f" (95% CI {ci[0]:.1%} to {ci[1]:.1%})" if ci else "")
        if w > 0:
            # Rounded data-end, square at the baseline.
            r = min(4.0, w)
            y = cy - BAR_H / 2
            out.append(
                f'<path class="bar" d="M{x0},{y:.1f} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} '
                f'v{BAR_H - 2 * r:.1f} a{r},{r} 0 0 1 -{r},{r} h-{w - r:.1f} z">'
                f"<title>{escape(tip)}</title></path>"
            )
        if ci:
            out.append(
                f'<line class="ci" x1="{x(ci[0]):.1f}" y1="{cy:.1f}" x2="{x(ci[1]):.1f}" '
                f'y2="{cy:.1f}"><title>{escape(tip)}</title></line>'
            )
        end = max(x(value), x(ci[1]) if ci else 0.0)
        out.append(f'<text class="val" x="{end + 6:.1f}" y="{cy + 4:.1f}">{value:.1%}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"
