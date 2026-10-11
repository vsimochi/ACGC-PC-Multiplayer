#!/usr/bin/env python3
"""generate_generic_icon.py - draws the project's GENERIC placeholder icon (icon_generic.png / icon_generic.ico) from scratch with Pillow primitives.

Original work for this project: a rounded teal square with two overlapping white circles (two players) and a small leaf. It deliberately uses no game artwork, no
game logo, no game font and no third-party image; it is plain geometry. The previous icon (icon.png / icon.ico, provenance unconfirmed, see THIRD_PARTY_NOTICES.md)
is kept in the repository untouched; app.rc selects which one is built into the executable.
Usage: python generate_generic_icon.py   (writes next to this script)"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S = 2048  # drawn large, then downscaled for smooth edges


def draw():
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # background: vertical teal gradient inside a rounded square
    bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    px = bg.load()
    for y in range(S):
        t = y / (S - 1)
        c = (int(38 + (24 - 38) * t), int(166 + (112 - 166) * t), int(154 + (120 - 154) * t), 255)
        for x in range(S):
            px[x, y] = c
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((60, 60, S - 60, S - 60), radius=420, fill=255)
    im.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(im)
    # two overlapping player circles
    d.ellipse((330, 560, 1130, 1360), fill=(255, 255, 255, 235))
    d.ellipse((918, 560, 1718, 1360), fill=(255, 255, 255, 235))
    # small leaf above the overlap (an ellipse polygon rotated by hand)
    import math
    pts = []
    cx, cy, a, b, ang = 1024, 440, 270, 120, math.radians(-35)
    for i in range(72):
        t = 2 * math.pi * i / 72
        x, y = a * math.cos(t), b * math.sin(t)
        pts.append((cx + x * math.cos(ang) - y * math.sin(ang), cy + x * math.sin(ang) + y * math.cos(ang)))
    d.polygon(pts, fill=(255, 255, 255, 255))
    # pedestal bar
    d.rounded_rectangle((420, 1500, 1628, 1620), radius=60, fill=(255, 255, 255, 235))
    return im


def main():
    big = draw()
    png = big.resize((512, 512), Image.LANCZOS)
    png.save(os.path.join(HERE, "icon_generic.png"))
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(HERE, "icon_generic.ico"), sizes=sizes)
    print("wrote icon_generic.png and icon_generic.ico")


if __name__ == "__main__":
    main()
