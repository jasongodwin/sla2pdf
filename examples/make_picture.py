"""Draw examples/images/landscape.png (a made-up picture: no third-party images).

    python examples/make_picture.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

W, H = 900, 600


def main() -> Path:
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):  # sky: dusk blue at the top to a warm orange at the horizon
        t = y / (H - 1)
        row = (round(40 + 215 * t), round(60 + 110 * t), round(140 - 60 * t))
        for x in range(W):
            px[x, y] = row
    d = ImageDraw.Draw(img)
    d.ellipse((560, 250, 760, 450), fill=(255, 214, 102))  # sun
    d.polygon([(0, 600), (0, 380), (180, 250), (360, 420), (520, 300), (720, 470),
               (900, 360), (900, 600)], fill=(52, 78, 65))  # hills
    d.polygon([(0, 600), (0, 470), (260, 400), (520, 500), (900, 430), (900, 600)],
              fill=(33, 52, 44))
    out = Path(__file__).parent / "images" / "landscape.png"
    out.parent.mkdir(exist_ok=True)
    img.save(out, optimize=True)
    return out


if __name__ == "__main__":
    print(main())
