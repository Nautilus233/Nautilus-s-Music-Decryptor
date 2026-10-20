"""Generate assets/icon.ico from assets/icon_source.png with Pillow.

    python assets/make_icon.py

源图是一张 Win98 风格的 32×32 像素画（纯色块 + 透明背景）。像素画必须用
**最近邻**缩放才能保持清晰：把每个源像素整块放大，边缘是硬的；用 LANCZOS 之类的
插值会把色块糊成一团渐变，反而"不清晰"。

* ≥ 原生尺寸：最近邻放大（64/128/256 是整数倍，48 是 1.5 倍，全部保持色块）；
* < 原生尺寸（16/24）：按 alpha 覆盖率决定像素去留、颜色取最近的原像素 —— 硬边降采样，
  轮廓还在，不会糊成灰边。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parent
SOURCE = OUT / "icon_source.png"
NATIVE = 32                                   # 源图原生尺寸（像素画一格一个像素）
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
PREVIEW = 256
ALPHA_CUT = 112                               # 降采样时的覆盖率阈值（0-255），>50% 才保留


def load_source() -> Image.Image:
    """读源图并统一成 RGBA/原生尺寸；缺图时给出可读错误。"""
    if not SOURCE.is_file():
        raise SystemExit(f"缺少图标源图：{SOURCE}")
    img = Image.open(SOURCE).convert("RGBA")
    if img.size != (NATIVE, NATIVE):
        img = img.resize((NATIVE, NATIVE), Image.NEAREST)
    return img


def _crisp_downscale(src: Image.Image, n: int) -> Image.Image:
    """硬边降采样到 n×n：alpha 取覆盖率，颜色取最近的原像素（不混色、不发灰）。"""
    alpha = src.getchannel("A").resize((n, n), Image.BOX)
    nearest = src.resize((n, n), Image.NEAREST)
    out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    sp, ap, op = nearest.load(), alpha.load(), out.load()
    for y in range(n):
        for x in range(n):
            if ap[x, y] >= ALPHA_CUT:
                r, g, b, _ = sp[x, y]
                op[x, y] = (r, g, b, 255)
    return out


def render(n: int) -> Image.Image:
    src = load_source()
    if n == NATIVE:
        return src
    if n > NATIVE:
        return src.resize((n, n), Image.NEAREST)
    return _crisp_downscale(src, n)


def main() -> None:
    base = render(PREVIEW)
    others = [render(n) for n in ICO_SIZES if n != PREVIEW]
    png = OUT / "icon.png"
    ico = OUT / "icon.ico"
    base.save(png)
    # 每个尺寸都显式给图，避免 Pillow 自己按 LANCZOS 缩放（那样就不清晰了）
    base.save(ico, format="ICO", sizes=[(n, n) for n in ICO_SIZES],
              append_images=others)
    print(f"wrote {ico} and {png}")


if __name__ == "__main__":
    main()
