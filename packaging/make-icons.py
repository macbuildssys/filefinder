"""Make the rounded app icons from your artwork. Needs Pillow: pip install pillow

Reads packaging/icons/filefinder-original.png (the square artwork), gives it rounded
corners with a see through background, and writes every size the packages use.
Run it again whenever the artwork changes.
"""
import math
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
MASTER = 1024
MARGIN = 0.04       # empty space around the shape, as a share of the width
ROUNDNESS = 5.0     # 2 is a circle, 4 to 5 is the soft rounded square used by modern icons


def rounded_mask(size: int) -> Image.Image:
    """A smooth rounded square, drawn large and shrunk so the edge has no jagged steps."""
    big = size * 4
    points = []
    for step in range(720):
        t = 2 * math.pi * step / 720
        x, y = math.cos(t), math.sin(t)
        px = math.copysign(abs(x) ** (2 / ROUNDNESS), x)
        py = math.copysign(abs(y) ** (2 / ROUNDNESS), y)
        points.append(((px + 1) / 2 * (big - 1), (py + 1) / 2 * (big - 1)))
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def build_master() -> Image.Image:
    art = Image.open(os.path.join(HERE, "icons", "filefinder-original.png")).convert("RGB")
    inner = round(MASTER * (1 - 2 * MARGIN))
    art = art.resize((inner, inner), Image.LANCZOS).convert("RGBA")
    art.putalpha(rounded_mask(inner))
    master = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    master.paste(art, ((MASTER - inner) // 2, (MASTER - inner) // 2), art)
    return master


def shrink(image: Image.Image, size: int) -> Image.Image:
    return image.convert("RGBa").resize((size, size), Image.LANCZOS).convert("RGBA")


def main():
    master = build_master()
    for size in SIZES:
        shrink(master, size).save(os.path.join(HERE, "icons", f"filefinder-{size}.png"), optimize=True)
    window = os.path.join(HERE, "..", "filefinder", "data", "filefinder.png")
    shrink(master, 256).save(window, optimize=True)
    print("Icons written for sizes:", ", ".join(str(s) for s in SIZES))


main()
