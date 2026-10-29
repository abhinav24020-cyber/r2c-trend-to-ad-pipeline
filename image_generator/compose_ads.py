"""
compose_ads.py — Turn a generated picture into a finished, branded, ready-to-post ad.

The image model paints the picture only (it misspells text). This module lays
out the R2C.Ai brand template around it, with exact spelling every time:

    [R2C.Ai logo]
    ( EYEBROW )                         royal-blue pill, e.g. "NEW OPEN MODEL"
    Headline                            Fraunces serif, large
    Subline                             Inter
    (v) proof point                     2-3 points with check marks
    (v) proof point
    [ CTA  -> ]                         royal-blue button
    ------------------------------------------------------------
    r2c.iiitd.edu.in          IIIT Delhi · Research to Commercialization

Brand look from r2c.iiitd.edu.in: navy #0A1F3C (logo), royal blue #3B5DE7
(theme), Fraunces headlines and Inter body text (both bundled, SIL OFL).
Tall formats put the picture on top and the text panel below; square and wide
formats put the text panel on the left and the picture on the right. The
picture fades into the navy panel so the two read as one design.

    python -m image_generator.compose_ads data/output/autopilot/<run>/generated_ads.json
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WEBSITE = os.environ.get("R2C_WEBSITE", "r2c.iiitd.edu.in")
FOOTER_LINE = "Incubated at IIIT Delhi"
SITE_LINE = "License Research & Technology from Indian Institutes"  # r2c.iiitd.edu.in description
HELP_TITLE = "HOW R2C.Ai HELPS YOU"
NAVY = os.environ.get("R2C_BRAND_PRIMARY", "#0A1F3C")      # R2C navy (logo colour): panel
NAVY_DEEP = "#06142A"
ACCENT = os.environ.get("R2C_BRAND_ACCENT", "#3B5DE7")     # R2C royal blue (site theme): pill, CTA, checks
ACCENT_SOFT = "#9DB0FF"
TEXT = "#FFFFFF"
SUBTEXT = "#C9D3EA"
MUTED = "#8C9BBA"
PRIMARY = NAVY  # kept for older callers

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
FONTS_DIR = os.path.join(ASSETS_DIR, "fonts")
LOGO_FILES = {"dark": "r2c_logo_white.png", "light": "r2c_logo_navy.png"}  # keyed by background tone

# Per ratio: picture and text boxes as fractions (x0, y0, x1, y1) of the canvas,
# "split" = which way the picture fades into the panel, and the footer's y.
LAYOUTS: Dict[str, Dict[str, Any]] = {
    "4:5": {"picture": (0, 0, 1, 0.50), "text": (0.07, 0.43, 0.93, 0.89), "split": "top", "footer": 0.95},
    "3:4": {"picture": (0, 0, 1, 0.48), "text": (0.07, 0.42, 0.93, 0.89), "split": "top", "footer": 0.95},
    "9:16": {"picture": (0, 0, 1, 0.50), "text": (0.08, 0.44, 0.92, 0.80), "split": "top", "footer": 0.84},
    "1:1": {"picture": (0.40, 0, 1, 1), "text": (0.06, 0.17, 0.54, 0.86), "split": "side", "footer": 0.94},
    "1.91:1": {"picture": (0.40, 0, 1, 1), "text": (0.045, 0.21, 0.53, 0.84), "split": "side", "footer": 0.93},
    "16:9": {"picture": (0.40, 0, 1, 1), "text": (0.045, 0.20, 0.53, 0.84), "split": "side", "footer": 0.93},
}
DEFAULT_LAYOUT = LAYOUTS["4:5"]

SYSTEM_FONTS = {
    "serif": ["C:/Windows/Fonts/georgiab.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"],
    "sans": ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"],
}
_FONT_CACHE: Dict[Tuple[str, int, int], Any] = {}


def _font(family: str, size: int, weight: int = 400):
    """Fraunces ("serif") or Inter ("sans") at a weight; system fonts if the bundled ones are missing."""
    key = (family, size, weight)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    path = os.path.join(FONTS_DIR, "Fraunces.ttf" if family == "serif" else "Inter.ttf")
    font = None
    try:
        font = ImageFont.truetype(path, size)
        axes = [a["name"] for a in font.get_variation_axes()]
        values = {b"Optical Size": min(144, max(9, size)), b"Optical size": min(32, max(14, size)),
                  b"Weight": weight, b"Softness": 30, b"Wonky": 0}
        font.set_variation_by_axes([values.get(n, 0) for n in axes])
    except (OSError, AttributeError, ValueError):
        font = None
        for candidate in SYSTEM_FONTS[family]:
            try:
                font = ImageFont.truetype(candidate, size)
                break
            except OSError:
                continue
        font = font or ImageFont.load_default(size=size)
    _FONT_CACHE[key] = font
    return font


def _hex(color: str, alpha: int = 255) -> Tuple[int, int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16), alpha


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> List[str]:
    words, lines, line = str(text or "").split(), [], ""
    for word in words:
        attempt = (line + " " + word).strip()
        if line and draw.textlength(attempt, font=font) > max_width:
            lines.append(line)
            line = word
        else:
            line = attempt
    if line:
        lines.append(line)
    return lines


def _box(size: Tuple[int, int], fractions: Sequence[float]) -> Tuple[int, int, int, int]:
    width, height = size
    return (int(width * fractions[0]), int(height * fractions[1]),
            int(width * fractions[2]), int(height * fractions[3]))


# --------------------------------------------------------------------------
# Background: navy canvas, brand glow, picture fading into the panel
# --------------------------------------------------------------------------
def _canvas(size: Tuple[int, int], layout: Dict[str, Any]) -> Image.Image:
    width, height = size
    canvas = Image.new("RGBA", size, _hex(NAVY))
    # Vertical navy -> deep navy gradient.
    shade = Image.linear_gradient("L").resize(size)
    canvas = Image.composite(Image.new("RGBA", size, _hex(NAVY_DEEP)), canvas, shade)
    # Soft royal-blue glow behind the text panel.
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    tx0, ty0, tx1, ty1 = _box(size, layout["text"])
    radius = int(max(width, height) * 0.32)
    cx, cy = (tx0 + int((tx1 - tx0) * 0.15), ty1) if layout["split"] == "top" else (tx0, ty1)
    ImageDraw.Draw(glow).ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=_hex(ACCENT, 70))
    canvas = Image.alpha_composite(canvas, glow.filter(ImageFilter.GaussianBlur(radius // 2)))
    # Faint dot grid in the panel corner away from the picture.
    dots = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(dots)
    step = max(12, width // 48)
    x_start = width - step * 7 if layout["split"] == "top" else tx1 - step * 7
    for gx in range(7):
        for gy in range(5):
            x, y = x_start + gx * step, height - step * (gy + 2)
            draw.ellipse((x - 1.5, y - 1.5, x + 1.5, y + 1.5), fill=_hex(ACCENT_SOFT, 60))
    return Image.alpha_composite(canvas, dots)


def _place_picture(canvas: Image.Image, picture: Image.Image, layout: Dict[str, Any]) -> Image.Image:
    """Cover-fit the picture into its box and fade its inner edge into the panel."""
    x0, y0, x1, y1 = _box(canvas.size, layout["picture"])
    box_w, box_h = x1 - x0, y1 - y0
    scale = max(box_w / picture.width, box_h / picture.height)
    resized = picture.convert("RGBA").resize((int(picture.width * scale + 1), int(picture.height * scale + 1)),
                                             Image.LANCZOS)
    left, top = (resized.width - box_w) // 2, (resized.height - box_h) // 2
    resized = resized.crop((left, top, left + box_w, top + box_h))
    mask = Image.new("L", (box_w, box_h), 255)
    ease = Image.linear_gradient("L").point(lambda v: int(255 * (v / 255.0) ** 1.6))  # eased, no visible seam
    if layout["split"] == "top":  # fade the bottom 45% into the panel
        fade = int(box_h * 0.45)
        mask.paste(ease.rotate(180).resize((box_w, fade)), (0, box_h - fade))
    else:  # fade the left half into the panel
        fade = int(box_w * 0.50)
        mask.paste(ease.rotate(90).resize((fade, box_h)), (0, 0))
    resized.putalpha(mask)
    canvas.alpha_composite(resized, (x0, y0))
    if layout["split"] == "top":  # darken the top so the white logo always reads
        top_shade = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        band = Image.linear_gradient("L").rotate(180).resize((canvas.width, int(canvas.height * 0.16)))
        top_shade.paste(Image.new("RGBA", band.size, _hex(NAVY_DEEP, 150)), (0, 0), band)
        canvas = Image.alpha_composite(canvas, top_shade)
    return canvas


def _logo(width: int) -> Image.Image:
    logo = Image.open(os.path.join(ASSETS_DIR, LOGO_FILES["dark"])).convert("RGBA")
    return logo.resize((width, int(logo.height * width / logo.width)), Image.LANCZOS)


# --------------------------------------------------------------------------
# Text block
# --------------------------------------------------------------------------
def _fit(draw, box_w: int, box_h: int, eyebrow: str, headline: str, subline: str,
         points: List[str], cta: str, base: float):
    """Largest type scale at which the whole text block fits its box."""
    for scale in [x / 100 for x in range(100, 35, -4)]:
        head = max(16, int(base * scale))
        f = {
            "eyebrow": _font("sans", max(11, int(head * 0.27)), 650),
            "head": _font("serif", head, 650),
            "sub": _font("sans", max(12, int(head * 0.37)), 400),
            "point": _font("sans", max(12, int(head * 0.34)), 500),
            "cta": _font("sans", max(12, int(head * 0.36)), 650),
        }
        head_lines = _wrap(draw, headline, f["head"], box_w)
        sub_lines = _wrap(draw, subline, f["sub"], box_w)
        point_lines = [_wrap(draw, p, f["point"], box_w - int(f["point"].size * 1.7)) for p in points]
        if len(head_lines) > 3 or len(sub_lines) > 3 or any(len(p) > 2 for p in point_lines):
            continue
        gap = int(head * 0.30)
        heights = {
            "eyebrow": int(f["eyebrow"].size * 2.1) if eyebrow else 0,
            "head": int(len(head_lines) * head * 1.08),
            "sub": int(len(sub_lines) * f["sub"].size * 1.35),
            "points": sum(int(len(p) * f["point"].size * 1.35) + int(gap * 0.55) for p in point_lines)
            + (int(f["eyebrow"].size * 1.9) if point_lines else 0),
            "cta": int(f["cta"].size * 2.6),
        }
        total = sum(heights.values()) + gap * (3 + (1 if points else 0)) + (gap if eyebrow else 0)
        if total <= box_h:
            return f, head_lines, sub_lines, point_lines, gap, total
    return f, head_lines[:3], sub_lines[:3], [p[:2] for p in point_lines], gap, total


def _check(draw, x: int, y: int, size: int) -> None:
    draw.ellipse((x, y, x + size, y + size), fill=_hex(ACCENT))
    w = max(2, size // 7)
    draw.line([(x + size * 0.28, y + size * 0.52), (x + size * 0.44, y + size * 0.68),
               (x + size * 0.73, y + size * 0.34)], fill=_hex(TEXT), width=w, joint="curve")


def _tracked(draw, xy: Tuple[int, int], text: str, font, fill, tracking: float) -> int:
    """Letter-spaced text (for the eyebrow); returns its width."""
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        x += draw.textlength(char, font=font) + tracking
    return int(x - xy[0] - tracking)


def _tracked_width(draw, text: str, font, tracking: float) -> int:
    return int(sum(draw.textlength(c, font=font) for c in text) + tracking * max(0, len(text) - 1))


def compose_ad(background_path: str, out_path: str, ratio: str, headline: str, subline: str, cta: str,
               eyebrow: str = "", points: Optional[Sequence[str]] = None) -> str:
    """Write the finished, branded ad PNG to out_path and return it."""
    picture = Image.open(background_path)
    size = picture.size
    width, height = size
    layout = LAYOUTS.get(ratio, DEFAULT_LAYOUT)
    canvas = _place_picture(_canvas(size, layout), picture, layout)
    draw = ImageDraw.Draw(canvas)
    tx0, ty0, tx1, ty1 = _box(size, layout["text"])
    box_w, box_h = tx1 - tx0, ty1 - ty0
    margin = int(min(width, height) * 0.055)

    # Logo: top-left, over the picture (tall formats) or atop the text column (wide formats).
    logo = _logo(int(width * (0.20 if layout["split"] == "top" else 0.17)))
    site = _font("sans", max(11, int(min(width, height) * 0.021)), 500)
    site_y = margin + logo.height + int(site.size * 0.5)
    if layout["split"] == "top":  # the logo block sits on the picture: give it a navy glass backing
        pad = int(site.size * 0.9)
        right = tx0 + max(logo.width, int(draw.textlength(SITE_LINE, font=site)))
        backing = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(backing).rounded_rectangle(
            (tx0 - pad, margin - pad, right + pad, site_y + int(site.size * 1.3) + pad),
            radius=pad, fill=_hex(NAVY_DEEP, 175))
        canvas.alpha_composite(backing)
        draw = ImageDraw.Draw(canvas)
    canvas.alpha_composite(logo, (tx0, margin))
    draw.text((tx0, site_y), SITE_LINE, font=site, fill=_hex(SUBTEXT))
    if layout["split"] == "side":
        ty0 = max(ty0, site_y + site.size + margin)
        box_h = ty1 - ty0

    points = [str(p).strip() for p in (points or []) if str(p).strip()][:3]
    eyebrow = str(eyebrow or "").strip().upper()
    base = box_w * (0.105 if layout["split"] == "top" else 0.13)
    f, head_lines, sub_lines, point_lines, gap, total = _fit(
        draw, box_w, box_h, eyebrow, headline, subline, points, cta, base)

    x = tx0
    y = ty0 + max(0, (box_h - total) // 2) if layout["split"] == "side" else ty0 + max(0, (box_h - total) // 3)
    if eyebrow:
        tracking = f["eyebrow"].size * 0.12
        pad_x, pill_h = int(f["eyebrow"].size * 0.9), int(f["eyebrow"].size * 2.1)
        pill_w = _tracked_width(draw, eyebrow, f["eyebrow"], tracking) + pad_x * 2
        draw.rounded_rectangle((x, y, x + pill_w, y + pill_h), radius=pill_h // 2,
                               fill=_hex(ACCENT, 60), outline=_hex(ACCENT_SOFT), width=max(1, pill_h // 18))
        _tracked(draw, (x + pad_x, y + int(pill_h * 0.22)), eyebrow, f["eyebrow"], _hex(TEXT), tracking)
        y += pill_h + gap
    for line in head_lines:
        draw.text((x, y), line, font=f["head"], fill=_hex(TEXT))
        y += int(f["head"].size * 1.08)
    y += gap
    for line in sub_lines:
        draw.text((x, y), line, font=f["sub"], fill=_hex(SUBTEXT))
        y += int(f["sub"].size * 1.35)
    if point_lines:
        y += gap
        _tracked(draw, (x, y), HELP_TITLE, f["eyebrow"], _hex(ACCENT_SOFT), f["eyebrow"].size * 0.12)
        y += int(f["eyebrow"].size * 1.9)
        mark = int(f["point"].size * 1.1)
        for lines in point_lines:
            _check(draw, x, y + int(f["point"].size * 0.12), mark)
            for line in lines:
                draw.text((x + int(mark * 1.55), y), line, font=f["point"], fill=_hex(TEXT))
                y += int(f["point"].size * 1.35)
            y += int(gap * 0.55)
    y += gap
    # CTA button with arrow.
    label = cta + "  →"
    pad_x, pad_y = int(f["cta"].size * 1.1), int(f["cta"].size * 0.62)
    cta_w = int(draw.textlength(label, font=f["cta"])) + pad_x * 2
    cta_h = int(f["cta"].size * 1.2) + pad_y * 2
    draw.rounded_rectangle((x, y, x + cta_w, y + cta_h), radius=cta_h // 2, fill=_hex(ACCENT))
    draw.text((x + pad_x, y + pad_y - int(f["cta"].size * 0.05)), label, font=f["cta"], fill=_hex(TEXT))

    # Footer: hairline, website, institute line.
    foot = _font("sans", max(11, int(min(width, height) * 0.022)), 500)
    fx0, fx1 = (tx0, tx1) if layout["split"] == "side" else (tx0, width - tx0)
    fy = int(height * layout["footer"]) - foot.size
    draw.line([(fx0, fy - int(foot.size * 0.9)), (fx1, fy - int(foot.size * 0.9))], fill=_hex(MUTED, 90), width=1)
    draw.text((fx0, fy), WEBSITE, font=_font("sans", foot.size, 650), fill=_hex(TEXT))
    right_w = draw.textlength(FOOTER_LINE, font=foot)
    if fx0 + draw.textlength(WEBSITE + "     ", font=foot) + right_w <= fx1:
        draw.text((fx1 - right_w, fy), FOOTER_LINE, font=foot, fill=_hex(MUTED))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    canvas.convert("RGB").save(out_path, "PNG")
    return out_path


# --------------------------------------------------------------------------
# Logo on an AI-drawn ad
# --------------------------------------------------------------------------
def _logo_box(size: Tuple[int, int], logo_size: Tuple[int, int], corner: str, margin: int) -> Tuple[int, int]:
    width, height = size
    lw, lh = logo_size
    vertical, _, horizontal = corner.partition("-")
    x = {"left": margin, "right": width - lw - margin}.get(horizontal, (width - lw) // 2)
    y = margin if vertical == "top" else height - lh - margin
    return x, y


def place_logo(image_path: str, out_path: str, corner: str = "top-left", scale: float = 0.22) -> str:
    """
    Stamp the official R2C logo into `corner` (e.g. "top-left", "bottom-right", "top-center").

    The logo is `scale` of the image's shorter side wide-ish (so it reads the same in
    every ratio); white on dark backgrounds, navy on light ones, judged from the
    brightness of the area it covers.
    """
    image = Image.open(image_path).convert("RGBA")
    width, height = image.size
    logo_w = int(min(width, height) * scale * 1.6)
    margin = int(min(width, height) * 0.045)
    sample = Image.open(os.path.join(ASSETS_DIR, LOGO_FILES["light"]))
    logo_h = int(sample.height * logo_w / sample.width)
    x, y = _logo_box((width, height), (logo_w, logo_h), corner, margin)
    region = image.crop((x, y, x + logo_w, y + logo_h)).convert("L")
    brightness = sum(region.getdata()) / float(logo_w * logo_h)
    tone = "dark" if brightness < 140 else "light"
    logo = Image.open(os.path.join(ASSETS_DIR, LOGO_FILES[tone])).convert("RGBA")
    logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
    image.alpha_composite(logo, (x, y))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    image.convert("RGB").save(out_path, "PNG")
    return out_path


def compose_record(record: Dict[str, Any], out_dir: Optional[str] = None) -> Optional[str]:
    """Compose the finished ad for one generated_ads.json record; returns its path or None."""
    post = record.get("social_post") or {}
    background = record.get("image_path")
    if record.get("status") != "success" or not background or not post.get("headline"):
        return None
    out_dir = out_dir or os.path.join(os.path.dirname(background), "final_ads")
    name = os.path.splitext(os.path.basename(background))[0] + "_ad.png"
    points = post.get("points") if isinstance(post.get("points"), list) else []
    return compose_ad(background, os.path.join(out_dir, name), record.get("aspect_ratio") or "1:1",
                      post.get("headline", ""), post.get("subline", ""), post.get("cta", "Learn More"),
                      eyebrow=post.get("eyebrow", ""), points=points)


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m image_generator.compose_ads <generated_ads.json>")
        return 2
    records = json.load(open(argv[0], encoding="utf-8"))
    made = [p for p in (compose_record(r) for r in records) if p]
    print("Composed {} finished ad(s).".format(len(made)))
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(main())
