#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_build_icon.py — 用 Pillow 绘制应用图标（渐变 + 玻璃高光 + "Q"），生成多尺寸 .ico。

为什么不用 QPainter：PySide6 离屏渲染时拿不到系统字体，画不出文字；
这里改用 Pillow + Windows 自带 TrueType 字体（构建期一次性用，不参与运行）。
打包时只在 venv 跑一次（需 Pillow）。运行 exe 本身不依赖此脚本。

用法: python _build_icon.py
产物: xk/build/app.ico（PyInstaller --icon 用）+ xk/build/app_icon_256.png（预览）
"""
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
BUILD_DIR = HERE / "build"
BUILD_DIR.mkdir(exist_ok=True)
SIZE = 256


def _find_font(prefer):
    """从 Windows/Fonts 找第一个存在的 ttf/ttc。"""
    fonts_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in prefer:
        p = fonts_dir / name
        if p.exists():
            return str(p)
    return None


def _vertical_gradient(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        r = int(0xA5 + (0x4F - 0xA5) * t)
        g = int(0xB4 + (0x46 - 0xB4) * t)
        b = int(0xFC + (0xE5 - 0xFC) * t)
        for x in range(size):
            px[x, y] = (r, g, b, 255)
    return img


def _clip_ellipse(img):
    size = img.size[0]
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((1, 1, size - 2, size - 2), fill=255)
    img.putalpha(mask)
    return img


def _draw_gloss(size):
    gh = int(size * 0.45)
    gloss = Image.new("RGBA", (size, gh), (0, 0, 0, 0))
    gd = ImageDraw.Draw(gloss)
    for y in range(gh):
        a = int(40 * (1 - y / gh) ** 1.4)
        gd.line([(0, y), (size, y)], fill=(255, 255, 255, a))
    mask = Image.new("L", (size, gh), 0)
    ImageDraw.Draw(mask).ellipse(
        (3, -int(size * 0.10), size - 3, int(size * 0.55)), fill=255)
    gloss.putalpha(mask)
    return gloss


def _draw_letter(img, font_path):
    if not font_path:
        return
    size = img.size[0]
    font = ImageFont.truetype(font_path, int(size * 0.52))
    d = ImageDraw.Draw(img)
    bbox = d.textbbox((0, 0), "Q", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) // 2 - bbox[0]
    y = (size - th) // 2 - bbox[1]
    d.text((x, y), "Q", fill=(255, 255, 255, 255), font=font)


def main():
    font_path = _find_font([
        "msyhbd.ttc", "simhei.ttf", "simsunb.ttf",
        "arialbd.ttf", "arial.ttf",
        "segoeuib.ttf", "seguisb.ttf",
    ])
    if not font_path:
        print("注意：未找到系统字体，将只画圆（文字省略）")

    base = _vertical_gradient(SIZE)
    base = _clip_ellipse(base)
    base.alpha_composite(_draw_gloss(SIZE), dest=(0, 2))
    _draw_letter(base, font_path)

    png_path = BUILD_DIR / "app_icon_256.png"
    base.save(str(png_path), format="PNG")

    ico_path = BUILD_DIR / "app.ico"
    base.save(str(ico_path), format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                     (64, 64), (128, 128), (256, 256)])
    print(f"生成 {ico_path}")
    print(f"生成 {png_path}（预览）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
