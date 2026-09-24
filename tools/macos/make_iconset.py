"""Makes the macOS app icon set from the repository's dark app icon.

    python tools/macos/make_iconset.py <source.png> <out.iconset>
    iconutil -c icns <out.iconset> -o AgnView.icns

The source is a rounded square on a black background, drawn for Windows. A
Mac icon needs the corners transparent and some margin, so this cuts the
rounded square out with a matching mask and centres it on Apple's 1024 point
grid, where the shape is 824 points wide.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

# The rounded square inside agnview-app-icon-dark.png, measured from the file,
# with a few pixels kept for its edge glow.
SHAPE_BOX = (112, 113, 1139, 1105)
SHAPE_RADIUS = 250
CANVAS = 1024
SHAPE_WIDTH = 824
SIZES = (16, 32, 128, 256, 512)


def master(source: Path) -> Image.Image:
    image = Image.open(source).convert("RGBA").crop(SHAPE_BOX)
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, image.width - 1, image.height - 1), SHAPE_RADIUS, fill=255)
    image.putalpha(mask)

    scale = SHAPE_WIDTH / image.width
    image = image.resize((SHAPE_WIDTH, round(image.height * scale)), Image.LANCZOS)
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    canvas.paste(image, ((CANVAS - image.width) // 2, (CANVAS - image.height) // 2), image)
    return canvas


def main(argv) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    source, target = Path(argv[1]), Path(argv[2])
    target.mkdir(parents=True, exist_ok=True)
    big = master(source)
    for size in SIZES:
        big.resize((size, size), Image.LANCZOS).save(target / f"icon_{size}x{size}.png")
        big.resize((size * 2, size * 2), Image.LANCZOS).save(target / f"icon_{size}x{size}@2x.png")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
