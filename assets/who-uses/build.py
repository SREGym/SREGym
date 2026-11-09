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
    gap, size = 13, 39
    start = (WIDTH - mark_width - gap - name_width) / 2
    items = [
        logo(filename, start, y - size / 2, mark_width, size, replacements=(("currentColor", color),)),
        text(name, start + mark_width + gap, y + 10, 28, color, weight=600),
    ]
    if note:
        items.append(text(note, WIDTH / 2, y + 34, 12, color, anchor="middle"))
    return "\n".join(items)


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
        "microsoft": (80, microsoft_logo(dark)),
        "datadog": (80, logo(f"datadog-{'white' if dark else 'purple'}.svg", 22, 15, 196, 48)),
        "cognition": (80, named_mark("cognition-0.svg", 40, 39, "Cognition", 131, ink)),
        "cloudthinker": (
            80,
            logo(
                "cloudthinker.svg",
                16,
                19,
                208,
                42,
                replacements=()
                if dark
                else (
                    ('fill="#fff" d="M103', f'fill="{ink}" d="M103'),
                    ('fill="#fff" d="M149', f'fill="{ink}" d="M149'),
                ),
            ),
        ),
        "nofire": (
            80,
            logo(
                "nofire.svg",
                25,
                22,
                190,
                37,
                replacements=(("white", "#0B0C0E"), ("#0B0C0D", "#FFFFFF")) if dark else (),
            ),
        ),
        "chiron": (80, named_mark("chiron.png", 40, 42, "Chiron", 90, ink)),
        "skyhook": (80, named_mark("skyhook.svg", 35, 58, "Skyhook", 116, ink, note="Radar")),
        "ciroos": (80, logo("ciroos.svg" if dark else "ciroos-color.png", 20, 16, 200, 48)),
        "washington": (100, logo(f"washington-stacked-{'white' if dark else 'purple'}.png", 27, 4, 186, 92)),
        "illinois": (
            100,
            logo(f"illinois-{'dark' if dark else 'light'}.svg", 2, 25, 236, 41)
            + text("Urbana-Champaign", 120, 89, 13, muted, anchor="middle"),
        ),
        "toronto": (100, logo("toronto.svg", 15, 10, 210, 79, replacements=() if dark else (("#FFFFFF", "#002A5C"),))),
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
