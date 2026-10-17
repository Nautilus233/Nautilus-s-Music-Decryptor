"""Generate assets/icon.ico (and a preview PNG) with Pillow.

    python assets/make_icon.py

Draws a rounded-square gradient badge with a music note whose "lock" has
popped open - the app's whole job in one glyph.  Rendered at 4x and
down-sampled for clean anti-aliasing.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent
SIZE = 256
SS = 4                      # supersampling factor
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _gradient(size: int) -> Image.Image:
    top, bottom = (37, 99, 235), (124, 58, 237)          # blue -> violet
    img = Image.new("RGB", (1, size))
    px = img.load()
    for y in range(size):
        t = y / max(1, size - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return img.resize((size, size))


def build() -> Image.Image:
    S = SIZE * SS
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # rounded-square gradient plate
    plate = _gradient(S).convert("RGBA")
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    base.paste(plate, (0, 0), mask)

    d = ImageDraw.Draw(base)
    white = (255, 255, 255, 255)
    blue = (37, 99, 235, 255)

    # ---- music note (eighth note, upper-left) --------------------------
    stem_w = int(S * 0.062)
    stem_x = int(S * 0.55)
    stem_top = int(S * 0.18)
    stem_bottom = int(S * 0.60)
    d.rounded_rectangle([stem_x, stem_top, stem_x + stem_w, stem_bottom],
                        radius=stem_w // 2, fill=white)

    head_r = int(S * 0.115)
    hx, hy = int(S * 0.40), int(S * 0.60)
    d.ellipse([hx - head_r, hy - head_r * 0.80, hx + head_r, hy + head_r * 0.80], fill=white)

    flag = [(stem_x + stem_w, stem_top),
            (int(S * 0.80), int(S * 0.25)),
            (int(S * 0.755), int(S * 0.40)),
            (stem_x + stem_w, stem_top + int(S * 0.10))]
    d.polygon(flag, fill=white)

    # ---- unlock badge (lower-right) ------------------------------------
    bc = int(S * 0.745)
    br = int(S * 0.235)
    d.ellipse([bc - br, bc - br, bc + br, bc + br], fill=white)

    lw, lh = int(br * 0.66), int(br * 0.52)
    lx, ly = bc - lw // 2, bc - int(br * 0.02)
    d.rounded_rectangle([lx, ly, lx + lw, ly + lh], radius=int(br * 0.13), fill=blue)

    aw = int(br * 0.44)
    ax, ay = bc - aw // 2 + int(br * 0.12), ly - int(br * 0.36)
    d.arc([ax, ay, ax + aw, ay + aw], start=180, end=360, fill=blue, width=int(br * 0.12))

    kr = int(br * 0.105)
    cx, cy = bc, ly + lh // 2
    d.ellipse([cx - kr, cy - kr, cx + kr, cy + kr], fill=white)

    return base.resize((SIZE, SIZE), Image.LANCZOS)

def main() -> None:
    img = build()
    png = OUT / "icon.png"
    img.save(png)
    img.save(OUT / "icon.ico", sizes=ICO_SIZES)
    print(f"wrote {OUT / 'icon.ico'} and {png}")


if __name__ == "__main__":
    main()