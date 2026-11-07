"""Draw the integration's brand icons (original artwork, not Cloudflare's logo).

Two endpoints, a phone and a camera, meet through a relay whose circular arrow
stands for credentials that renew themselves. Run from the repository root:

    python script/brand.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "custom_components/cloudflare_turn/brand"
SCALE = 4  # draw large, then downsample for smooth edges
ORANGE = (244, 129, 32, 255)
WHITE = (255, 255, 255, 255)
ENDPOINTS = {"": (55, 71, 79, 255), "dark_": (236, 239, 241, 255)}


def _draw(endpoint_color: tuple[int, int, int, int]) -> Image.Image:
    size = 1024 * SCALE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def s(v: float) -> int:
        return round(v * SCALE)

    relay, relay_r = (512, 340), 236
    ends, end_r = [(170, 836), (854, 836)], 118
    link_w = 64
    for ex, ey in ends:
        # Link from the endpoint towards the relay, with rounded ends.
        dx, dy = relay[0] - ex, relay[1] - ey
        length = math.hypot(dx, dy)
        ux, uy = dx / length, dy / length
        x1, y1 = ex + ux * (end_r - 10), ey + uy * (end_r - 10)
        x2, y2 = relay[0] - ux * (relay_r - 10), relay[1] - uy * (relay_r - 10)
        d.line((s(x1), s(y1), s(x2), s(y2)), fill=endpoint_color, width=s(link_w))
        for cx, cy in ((x1, y1), (x2, y2)):
            r = link_w / 2
            d.ellipse((s(cx - r), s(cy - r), s(cx + r), s(cy + r)), fill=endpoint_color)
        d.ellipse(
            (s(ex - end_r), s(ey - end_r), s(ex + end_r), s(ey + end_r)),
            fill=endpoint_color,
        )
    # The relay, on top of the links.
    rx, ry = relay
    d.ellipse(
        (s(rx - relay_r), s(ry - relay_r), s(rx + relay_r), s(ry + relay_r)),
        fill=ORANGE,
    )
    # Circular "renew" arrow inside the relay.
    arc_r, arc_w = 128, 46
    start, end = -60, 230  # degrees, clockwise from 3 o'clock in Pillow
    d.arc(
        (s(rx - arc_r), s(ry - arc_r), s(rx + arc_r), s(ry + arc_r)),
        start=start,
        end=end,
        fill=WHITE,
        width=s(arc_w),
    )
    # Arrowhead at the arc's start, pointing against the drawing direction.
    a = math.radians(start)
    mid = arc_r - arc_w / 2
    tip_base = (rx + mid * math.cos(a), ry + mid * math.sin(a))
    tangent = (math.sin(a), -math.cos(a))  # counter-clockwise direction
    normal = (math.cos(a), math.sin(a))
    head_len, head_half = 92, 70
    tip = (tip_base[0] + tangent[0] * head_len, tip_base[1] + tangent[1] * head_len)
    left = (tip_base[0] + normal[0] * head_half, tip_base[1] + normal[1] * head_half)
    right = (tip_base[0] - normal[0] * head_half, tip_base[1] - normal[1] * head_half)
    d.polygon([(s(x), s(y)) for x, y in (tip, left, right)], fill=WHITE)
    return img


def _square_trim(img: Image.Image) -> Image.Image:
    """Crop to the drawing, centred on a square canvas with no spare border."""
    box = img.getbbox()
    assert box is not None
    crop = img.crop(box)
    side = max(crop.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(crop, ((side - crop.width) // 2, (side - crop.height) // 2))
    return square


def main() -> None:
    """Write icon.png, icon@2x.png and their dark-background variants."""
    OUT.mkdir(parents=True, exist_ok=True)
    for prefix, color in ENDPOINTS.items():
        art = _square_trim(_draw(color))
        for name, px in (("icon", 256), ("icon@2x", 512)):
            path = OUT / f"{prefix}{name}.png"
            art.resize((px, px), Image.Resampling.LANCZOS).save(path, optimize=True)
            print(path.name, px)


if __name__ == "__main__":
    main()
