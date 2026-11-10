"""Build individual transparent logos for the README's light and dark themes.

Run from any directory with Python 3 and rsvg-convert installed.
The build uses only saved assets and makes no network or model calls.
"""

import base64
import subprocess
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGOS = ROOT / "logos"
WIDTH = 240
HEIGHT = 90
SCALE = 3


def logo(filename, x, y, width, height, *, replacements=()):
    data = (LOGOS / filename).read_bytes()
    if filename.endswith(".svg"):
        svg = data.decode()
        for old, new in replacements:
            svg = svg.replace(old, new)
        data = svg.encode()
        mime = "image/svg+xml"
    else:
        mime = "image/png"
    encoded = base64.b64encode(data).decode()
    return (
        f'<image x="{x}" y="{y}" width="{width}" height="{height}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'href="data:{mime};base64,{encoded}"/>'
    )


def text(content, x, y, size, color, *, weight=400, anchor="start"):
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" '
        f'font-family="Arial, Helvetica, sans-serif" font-weight="{weight}" '
        f'fill="{color}" text-anchor="{anchor}">{escape(content)}</text>'
    )


def named_mark(filename, y, mark_width, name, name_width, color, *, note=None):
    gap, size = 11, 34
    start = (WIDTH - mark_width - gap - name_width) / 2
    items = [
        logo(filename, start, y - size / 2, mark_width, size, replacements=(("currentColor", color),)),
        text(name, start + mark_width + gap, y + 9, 27, color, weight=600),
    ]
    if note:
        items.append(text(note, WIDTH / 2, y + 31, 11, color, anchor="middle"))
    return "\n".join(items)


def centered_logo(filename, width, height, *, replacements=()):
    return logo(filename, (WIDTH - width) / 2, (HEIGHT - height) / 2, width, height, replacements=replacements)


def washington_logo(dark, ink, muted):
    return (
        logo(
            "washington-w.svg",
            30,
            31,
            42,
            29,
            replacements=(("#4b2e83", "#FFFFFF"), ("#4B2E83", "#FFFFFF")) if dark else (),
        )
        + text("University of", 84, 37, 14, muted)
        + text("Washington", 84, 60, 22, ink, weight=500)
    )


def microsoft_logo(dark):
    original = logo("microsoft.png", 14, 16, 212, 45)
    if not dark:
        return original
    # Preserve the four brand colors and make only the wordmark white.
    return (
        '<defs><clipPath id="microsoft-wordmark"><rect x="65" y="16" '
        'width="162" height="45"/></clipPath>'
        '<filter id="white-lettering"><feColorMatrix type="matrix" '
        'values="0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 0 1 0"/>'
        "</filter></defs>"
        + original
        + '<g clip-path="url(#microsoft-wordmark)" filter="url(#white-lettering)">'
        + original
        + "</g>"
    )


def build(theme):
    dark = theme == "dark"
    ink = "#f0f6fc" if dark else "#1f2328"
    muted = "#aeb7c2" if dark else "#59636e"
    organizations = {
        "microsoft": (
            HEIGHT,
            '<g transform="translate(120 45) scale(0.70) translate(-120 -38.5)">' + microsoft_logo(dark) + "</g>",
        ),
        "datadog": (HEIGHT, centered_logo(f"datadog-{'white' if dark else 'purple'}.svg", 185, 46)),
        "cognition": (HEIGHT, named_mark("cognition-0.svg", 45, 34, "Cognition", 126, ink)),
        "komodor": (
            HEIGHT,
            centered_logo("komodor.svg", 174, 30, replacements=() if dark else (("#FF8F6B", ink),)),
        ),
        "cloudthinker": (
            HEIGHT,
            centered_logo(
                "cloudthinker.svg",
                232,
                47,
                replacements=()
                if dark
                else (
                    ('fill="#fff" d="M103', f'fill="{ink}" d="M103'),
                    ('fill="#fff" d="M149', f'fill="{ink}" d="M149'),
                ),
            ),
        ),
        "nofire": (
            HEIGHT,
            centered_logo(
                "nofire.svg",
                180,
                35,
                replacements=(("white", "#0B0C0E"), ("#0B0C0D", "#FFFFFF")) if dark else (),
            ),
        ),
        "chiron": (HEIGHT, named_mark("chiron.png", 45, 34, "Chiron", 86, ink)),
        "skyhook": (HEIGHT, named_mark("skyhook.svg", 42, 45, "Skyhook", 112, ink, note="Radar")),
        "ciroos": (HEIGHT, centered_logo("ciroos.svg" if dark else "ciroos-color.png", 180, 44)),
        "one2n": (
            HEIGHT,
            centered_logo(
                "one2n.svg",
                126,
                45,
                replacements=(('viewBox="0 0 118 50"', 'viewBox="14 6 98 35"'),)
                + ((("rgb(6, 16, 89)", ink),) if dark else ()),
            ),
        ),
        "exo": (HEIGHT, named_mark(f"exo-{'dark' if dark else 'light'}.svg", 45, 38, "Exo", 51, ink)),
        "p95": (
            HEIGHT,
            centered_logo(
                "p95.svg",
                165,
                29,
                replacements=() if dark else (("#d1e394", ink), ("#d1d7d9", ink)),
            ),
        ),
        "bespoke": (
            HEIGHT,
            centered_logo(
                "bespoke.svg",
                214,
                30,
                replacements=() if dark else (("rgb(255, 255, 255)", ink),),
            ),
        ),
        "washington": (HEIGHT, washington_logo(dark, ink, muted)),
        "illinois": (
            HEIGHT,
            logo(f"illinois-{'dark' if dark else 'light'}.svg", 38, 28, 164, 28)
            + text("Urbana-Champaign", 120, 72, 11, muted, anchor="middle"),
        ),
        "toronto": (
            HEIGHT,
            centered_logo("toronto.svg", 160, 59, replacements=() if dark else (("#FFFFFF", "#002A5C"),)),
        ),
        "berkeley": (HEIGHT, centered_logo(f"berkeley-{'dark' if dark else 'light'}.png", 144, 46)),
        "asu": (
            HEIGHT,
            centered_logo("asu-white.png" if dark else "asu-color.png", 210 if dark else 185, 60 if dark else 40),
        ),
    }
    output = ROOT / theme
    output.mkdir(exist_ok=True)
    for name, (height, content) in organizations.items():
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
            f'height="{height}" viewBox="0 0 {WIDTH} {height}">'
            f"{content}</svg>"
        )
        subprocess.run(
            [
                "rsvg-convert",
                "--width",
                str(WIDTH * SCALE),
                "--height",
                str(height * SCALE),
                "--output",
                str(output / f"{name}.png"),
            ],
            input=svg.encode(),
            check=True,
        )
    print(f"Built {len(organizations)} transparent {theme} logos.")


if __name__ == "__main__":
    for theme in ("light", "dark"):
        build(theme)
