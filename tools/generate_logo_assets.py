from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = PROJECT_ROOT / "assets" / "icons"
ICONSET_DIR = ICON_DIR / "AudioRouter.iconset"


def rounded_rectangle_background(image: Image.Image) -> None:
    width, height = image.size
    draw = ImageDraw.Draw(image, "RGBA")

    for y in range(height):
        blend = y / (height - 1)
        r = int(15 + (29 - 15) * blend)
        g = int(23 + (78 - 23) * blend)
        b = int(42 + (216 - 42) * blend)
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    inset = int(width * 0.07)
    radius = int(width * 0.2)
    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        [inset, inset, width - inset, height - inset],
        radius=radius,
        fill=255,
    )

    rounded = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    rounded.paste(image, (0, 0), mask)
    image.paste(rounded)


def draw_logo(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded_rectangle_background(image)

    draw = ImageDraw.Draw(image, "RGBA")

    scale = size / 1024
    speaker = [280, 430, 400, 590]
    draw.rectangle(
        [speaker[0] * scale, speaker[1] * scale, speaker[2] * scale, speaker[3] * scale],
        fill=(248, 250, 252, 255),
    )
    draw.polygon(
        [
            (400 * scale, 430 * scale),
            (570 * scale, 300 * scale),
            (570 * scale, 720 * scale),
            (400 * scale, 590 * scale),
        ],
        fill=(248, 250, 252, 255),
    )

    arc_bbox_outer = [560 * scale, 360 * scale, 860 * scale, 660 * scale]
    arc_bbox_inner = [610 * scale, 420 * scale, 780 * scale, 600 * scale]
    draw.arc(arc_bbox_outer, start=-48, end=48, fill=(103, 232, 249, 255), width=max(8, int(46 * scale)))
    draw.arc(arc_bbox_inner, start=-44, end=44, fill=(165, 243, 252, 255), width=max(6, int(34 * scale)))

    out1 = int(34 * scale)
    out2 = int(22 * scale)
    cx1, cy1 = int(808 * scale), int(512 * scale)
    cx2, cy2 = int(882 * scale), int(512 * scale)
    draw.ellipse([cx1 - out1, cy1 - out1, cx1 + out1, cy1 + out1], fill=(248, 250, 252, 255))
    draw.ellipse([cx2 - out2, cy2 - out2, cx2 + out2, cy2 + out2], fill=(248, 250, 252, 192))

    return image


def generate_png_and_ico() -> None:
    ICON_DIR.mkdir(parents=True, exist_ok=True)

    base = draw_logo(1024)
    png_path = ICON_DIR / "audio-router.png"
    base.save(png_path, format="PNG")

    ico_path = ICON_DIR / "audio-router.ico"
    base.save(
        ico_path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


def generate_icns_if_possible() -> None:
    iconutil = shutil.which("iconutil")
    if not iconutil:
        print("iconutil not found; skipping ICNS generation.")
        return

    if ICONSET_DIR.exists():
        shutil.rmtree(ICONSET_DIR)
    ICONSET_DIR.mkdir(parents=True, exist_ok=True)

    size_map = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]

    for size, filename in size_map:
        draw_logo(size).save(ICONSET_DIR / filename, format="PNG")

    subprocess.run(
        [iconutil, "-c", "icns", str(ICONSET_DIR), "-o", str(ICON_DIR / "audio-router.icns")],
        check=True,
    )


def main() -> None:
    generate_png_and_ico()
    generate_icns_if_possible()
    print(f"Icons generated in: {ICON_DIR}")


if __name__ == "__main__":
    main()
