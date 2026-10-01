"""One-off builder for the static social-share (OG) image.

Not part of the running app — run manually (via rsvg-convert, which is a
build-time tool, not a runtime dependency) whenever the brand mark or copy
changes, and commit the resulting PNG under web/static/.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

W, H = 1200, 630
PAPER = "#faf8f3"
INK = "#16151b"
MUTED = "#605d68"
FAINT = "#6c6a75"
LINE = "#ebe6dc"
HIGHLIGHT = "#f2b64a"
HIGHLIGHT_INK = "#945f00"
HIGHLIGHT_SOFT = "#fff2d1"

CATS = [
    ("Models", "#3b37c9"),
    ("Lawsuits & policy", "#c9463b"),
    ("Funding", "#1f9e6a"),
    ("Research", "#7b3fbf"),
    ("Industry moves", "#b56b1f"),
]

SERIF = "Charter, Georgia, serif"
SANS = "Liberation Sans, DejaVu Sans, sans-serif"
MONO = "DejaVu Sans Mono, monospace"

# 7-bar signal mark: 6 muted bars + 1 lit (gold) bar, matching the header
# wordmark's geometry (site.css .sw-logo), scaled up 4x and recoloured for
# a light-background card.
BAR_X = [2, 6.5, 11, 15.5, 20, 24.5, 29]
BAR_Y = [14, 11, 16, 6, 13, 9, 15]
BAR_H = [10, 13, 8, 18, 11, 15, 9]
LIT_INDEX = 3


def logo_svg(x: float, y: float, scale: float) -> str:
    parts = [f'<g transform="translate({x} {y}) scale({scale})">']
    for i, (bx, by, bh) in enumerate(zip(BAR_X, BAR_Y, BAR_H)):
        fill = HIGHLIGHT if i == LIT_INDEX else INK
        opacity = 1 if i == LIT_INDEX else 0.32
        parts.append(
            f'<rect x="{bx}" y="{by}" width="2.4" height="{bh}" rx="1.2" '
            f'fill="{fill}" opacity="{opacity}"/>'
        )
    parts.append("</g>")
    return "".join(parts)


def cat_pill(x: float, y: float, label: str, color: str) -> tuple[str, float]:
    pad_x, dot_r, gap, font_size = 22, 6, 12, 22
    text_w = len(label) * (font_size * 0.56)
    width = pad_x * 2 + dot_r * 2 + gap + text_w
    height = 48
    escaped = label.replace("&", "&amp;")
    svg = f"""
    <g transform="translate({x} {y})">
      <rect x="0" y="0" width="{width:.0f}" height="{height}" rx="{height/2:.0f}"
            fill="#ffffff" stroke="{LINE}" stroke-width="1.5"/>
      <circle cx="{pad_x + dot_r}" cy="{height/2:.0f}" r="{dot_r}" fill="{color}"/>
      <text x="{pad_x*2 + dot_r*2}" y="{height/2 + font_size*0.34:.0f}"
            font-family="{SANS}" font-size="{font_size}" font-weight="600"
            fill="{MUTED}">{escaped}</text>
    </g>"""
    return svg, width


def build() -> str:
    logo = logo_svg(88, 150, 2.9)

    cats_svg = []
    cx = 88
    cy = H - 118
    for label, color in CATS:
        svg, w = cat_pill(cx, cy, label, color)
        cats_svg.append(svg)
        cx += w + 14

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}"
     viewBox="0 0 {W} {H}">
  <rect width="{W}" height="{H}" fill="{PAPER}"/>

  <!-- eyebrow pill -->
  <g transform="translate(88 70)">
    <rect x="0" y="0" width="286" height="40" rx="20" fill="{HIGHLIGHT_SOFT}"
          stroke="{HIGHLIGHT}" stroke-opacity="0.35"/>
    <circle cx="22" cy="20" r="5" fill="{HIGHLIGHT}"/>
    <text x="38" y="26" font-family="{SANS}" font-size="16" font-weight="700"
          letter-spacing="1.6" fill="{HIGHLIGHT_INK}">WEEKLY &#183; CURATED &#183; CALM</text>
  </g>

  {logo}
  <text x="220" y="222" font-family="{SERIF}" font-size="72" font-weight="700"
        letter-spacing="-1" fill="{INK}">Signalweek</text>

  <text font-family="{SERIF}" font-size="42" font-weight="600" fill="{INK}">
    <tspan x="88" y="300">The AI industry in five sections.</tspan>
    <tspan x="88" y="354">Every item cites its primary source.</tspan>
  </text>

  <text x="88" y="410" font-family="{SANS}" font-size="25" fill="{MUTED}">No accounts, no infinite scroll &#8212; one calm page every Monday.</text>

  <line x1="88" y1="454" x2="{W - 88}" y2="454" stroke="{LINE}" stroke-width="1.5"/>

  {''.join(cats_svg)}
</svg>"""


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "src" / "signalweek" / "web" / "static"
    svg_path = out_dir / "og-image.svg"
    png_path = out_dir / "og-image.png"
    svg_path.write_text(build())
    subprocess.run(
        ["rsvg-convert", "-w", str(W), "-h", str(H), "-o", str(png_path), str(svg_path)],
        check=True,
    )
    svg_path.unlink()
    print(f"wrote {png_path}")


if __name__ == "__main__":
    main()
