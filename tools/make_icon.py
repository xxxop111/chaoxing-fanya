# -*- coding: utf-8 -*-
"""生成 ``chaoxing.ico``（exe / 任务管理器 / 任务栏 / 托盘 / 快捷方式 / 安装包共用的那一个）。

造型：蓝渐变圆角方块 + 白色「学」字。

为什么要按尺寸逐个重画：把 256×256 直接缩到 16×16，「学」的横画会糊成一团。
这里每个尺寸都在 4× 超采样画布上重新绘制、再用 LANCZOS 降采样，小尺寸下笔画仍分得开。

用法::

    .venv\\Scripts\\python.exe tools\\make_icon.py                 # 覆盖根目录 chaoxing.ico
    .venv\\Scripts\\python.exe tools\\make_icon.py --out _icon_candidates\\B-xue.ico
    .venv\\Scripts\\python.exe tools\\make_icon.py --glyph 学 --top 2563EB --bottom 3B82F6

换完图标要重打包、重装才看得到（`tools/build_desktop.py` + `tools/build_installer.py`）；
Windows 会缓存图标，必要时重启一次资源管理器。
"""

from __future__ import annotations

import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

SIZES = (256, 128, 64, 48, 32, 16)
SUPERSAMPLE = 4            # 每个尺寸先按 N 倍画再缩小
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",   # 微软雅黑 Bold
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
)
DEFAULT_GLYPH = "学"
DEFAULT_TOP = "2563EB"
DEFAULT_BOTTOM = "3B82F6"
CORNER_RATIO = 0.225       # 圆角半径 / 边长（Windows 11 风格方块大约就是这么多）
GLYPH_RATIO = 0.60         # 字号 / 边长


def _rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) != 6:
        raise SystemExit(f"颜色要写成 RRGGBB：{value!r}")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _font_path() -> str:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    raise SystemExit("找不到中文字体（试过 msyhbd/msyh/simhei），用 --font 指定")


def _base(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    """蓝渐变圆角方块。"""
    w = size * SUPERSAMPLE
    strip = Image.new("RGB", (1, w))
    for y in range(w):
        t = y / (w - 1)
        strip.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    grad = strip.resize((w, w))

    mask = Image.new("L", (w, w), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, w - 1],
                                           radius=int(w * CORNER_RATIO), fill=255)
    tile = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    tile.paste(grad, (0, 0), mask)
    return tile


def _with_glyph(tile: Image.Image, glyph: str, font_path: str) -> Image.Image:
    w = tile.width
    layer = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    font = ImageFont.truetype(font_path, int(w * GLYPH_RATIO))
    ImageDraw.Draw(layer).text((w / 2, w / 2 + w * 0.012), glyph, font=font,
                               fill=(255, 255, 255, 255), anchor="mm")
    tile.alpha_composite(layer)
    return tile


def render(size: int, glyph: str, font_path: str,
           top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    tile = _with_glyph(_base(size, top, bottom), glyph, font_path)
    return tile.resize((size, size), Image.LANCZOS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 chaoxing.ico")
    parser.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chaoxing.ico"),
        help="输出 .ico 路径（默认仓库根目录 chaoxing.ico）")
    parser.add_argument("--glyph", default=DEFAULT_GLYPH, help="方块里的字（默认 学）")
    parser.add_argument("--top", default=DEFAULT_TOP, help="渐变色上端 RRGGBB")
    parser.add_argument("--bottom", default=DEFAULT_BOTTOM, help="渐变色下端 RRGGBB")
    parser.add_argument("--font", default=None, help="字形字体文件（默认系统雅黑 Bold）")
    args = parser.parse_args(argv)

    font_path = args.font or _font_path()
    images = [render(size, args.glyph, font_path, _rgb(args.top), _rgb(args.bottom))
              for size in SIZES]
    images[0].save(args.out, format="ICO", append_images=images[1:])

    with Image.open(args.out) as check:
        sizes = sorted(check.info.get("sizes", []))
    print(f"[ok] {args.out}（{os.path.getsize(args.out)} 字节，尺寸 {sizes}）")
    if len(sizes) != len(SIZES):
        print(f"[warn] 期望 {len(SIZES)} 个尺寸，实际 {len(sizes)} 个", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
