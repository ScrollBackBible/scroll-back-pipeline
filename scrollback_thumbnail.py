#!/usr/bin/env python3
"""
Scroll Back episode thumbnail
=============================

Puts the thumbnail text on a piece of generated art, to the production bible's
thumbnail spec. Episode 2's thumbnail (JESUS MADE A WHIP?) was made with exactly
these settings.

    python3 scrollback_thumbnail.py ART.png "JESUS MADE|A WHIP?" E2F1_thumb.jpg

  ART.png   the art: Ollie mid-panic, large in the left third, the scene blurred
            behind him, no text (GPT Image 2.5 in Higgsfield, 16:9, high, 2k,
            character sheet + Jesus image as references)
  TEXT      3-5 words in question form; "|" starts a new line. Set in caps.
  OUT.jpg   1280x720 JPEG, checked to be under YouTube's 2MB limit

Needs Python 3 with Pillow, and Montserrat-ExtraBold.ttf next to this script
(or --font PATH). It refuses any other font, like the assembly pipeline does.
Font file: https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf
SHA-256 d3ac6a843d3ba6d5cafd44cf39e437055c8aed7e261010f595f57d3c7b3e2c1b
"""

import argparse
import hashlib
import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_SHA256 = "d3ac6a843d3ba6d5cafd44cf39e437055c8aed7e261010f595f57d3c7b3e2c1b"

THUMB = {                      # the bible's thumbnail spec, in px at 1280x720
    "size": (1280, 720),
    "cream": (246, 240, 230, 255),        # #F6F0E6
    "text_x0": 0.36,                      # text area: from 36% of the width ...
    "text_right_margin": 40,              # ... to 40px from the right edge
    "max_size": 150,                      # largest type size; steps down by 4 until the longest line fits
    "tracking": 0.04,                     # letter-spacing, as a fraction of the type size
    "line_height": 1.02,                  # line to line, as a multiple of the type size
    "shadow_alpha": 190,                  # black at 75%
    "shadow_blur": 12,
    "shadow_dy": 7,
    "shade_from": 0.30,                   # darkening across the text side: starts at 30% of the width ...
    "shade_ramp": 0.45,                   # ... reaches full strength 45% of the width later ...
    "shade_max": 115,                     # ... at black 45%
    "jpeg_quality": 92,
    "max_bytes": 2 * 1024 * 1024,
}


def check_font(path):
    if not os.path.exists(path):
        sys.exit(f"font not found: {path}\nDownload Montserrat-ExtraBold.ttf (see the top of this file).")
    family, style = ImageFont.truetype(path, 20).getname()
    if (family, style) != ("Montserrat", "ExtraBold"):
        sys.exit(f"{path} is {family} {style}, not Montserrat ExtraBold. The channel uses one typeface everywhere.")
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if digest != FONT_SHA256:
        print(f"note: {path} is Montserrat ExtraBold but a different build (sha256 {digest[:16]}...)", file=sys.stderr)
    return family, style


def make_thumbnail(art, text, out, font, spec=THUMB):
    W, H = spec["size"]
    lines = [t.strip().upper() for t in text.split("|") if t.strip()]
    im = Image.open(art).convert("RGB")
    s = max(W / im.width, H / im.height)                       # fill the frame, centre crop
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    left, top = (im.width - W) // 2, (im.height - H) // 2
    im = im.crop((left, top, left + W, top + H)).convert("RGBA")

    ramp = Image.new("L", (W, 1))                              # gentle darkening behind the text
    for x in range(W):
        k = max(0.0, min(1.0, (x / W - spec["shade_from"]) / spec["shade_ramp"]))
        ramp.putpixel((x, 0), int(k * spec["shade_max"]))
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shade.putalpha(ramp.resize((W, H)))
    im = Image.alpha_composite(im, shade)

    x0, x1 = int(W * spec["text_x0"]), W - spec["text_right_margin"]
    tr = spec["tracking"]

    def width(f, t):
        return f.getlength(t) + f.size * tr * max(0, len(t) - 1)

    size = spec["max_size"]
    while size > 60:
        f = ImageFont.truetype(font, size)
        if max(width(f, t) for t in lines) <= x1 - x0:
            break
        size -= 4
    asc, _ = f.getmetrics()
    lh = int(size * spec["line_height"])
    y0 = (H - (lh * (len(lines) - 1) + asc)) // 2              # block centred top to bottom
    fg = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)
    for i, t in enumerate(lines):
        x = x0 + ((x1 - x0) - width(f, t)) / 2                 # each line centred in the text area
        y = y0 + i * lh - (asc - size * 0.74)
        for j, ch in enumerate(t):
            d.text((x + f.getlength(t[:j]) + j * size * tr, y), ch, font=f, fill=spec["cream"])
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh.putalpha(fg.split()[3].point(lambda v: v * spec["shadow_alpha"] // 255))
    sh = sh.filter(ImageFilter.GaussianBlur(spec["shadow_blur"]))
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.alpha_composite(sh, (0, spec["shadow_dy"]))
    im = Image.alpha_composite(im, layer)
    im = Image.alpha_composite(im, fg)
    im.convert("RGB").save(out, quality=spec["jpeg_quality"], optimize=True)
    n = os.path.getsize(out)
    if n > spec["max_bytes"]:
        sys.exit(f"{out} is {n} bytes, over YouTube's 2MB thumbnail limit")
    return size, n


def main():
    ap = argparse.ArgumentParser(description="Scroll Back thumbnail: art + text -> 1280x720 JPEG")
    ap.add_argument("art")
    ap.add_argument("text", help='e.g. "JESUS MADE|A WHIP?"  ("|" = new line)')
    ap.add_argument("out")
    ap.add_argument("--font", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "Montserrat-ExtraBold.ttf"))
    a = ap.parse_args()
    family, style = check_font(a.font)
    words = len(a.text.replace("|", " ").split())
    if not 3 <= words <= 5:
        print(f"note: {words} words; the spec is 3-5", file=sys.stderr)
    size, n = make_thumbnail(a.art, a.text, a.out, a.font)
    print(f"{a.out}: 1280x720, {n // 1024} KB, {family} {style} at {size}px")


if __name__ == "__main__":
    main()
