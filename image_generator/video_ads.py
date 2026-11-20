"""
video_ads.py — Branded motion-graphics video ads (Reels / Stories / feed video).

Turns an ad's picture and copy into a short MP4, with no extra AI cost and the
text always spelled correctly (it is typeset by code, as in compose_ads):

    0.0s  picture with a slow cinematic zoom; R2C.Ai logo fades in
    0.5s  hook pill slides in, sticker pops
    1.3s  headline reveals line by line, then the subline
    4.4s  "How R2C.Ai helps you" service chips appear one by one
    7.6s  the colour-theme end card slides up: logo, CTA button, website

    python -m image_generator.video_ads   (renders a demo from the first showcase ad)
"""
from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter

from .compose_ads import (ACCENT, ASSETS_DIR, FOOTER_LINE, LOGO_FILES, NAVY, POSTER_THEMES, SITE_LINE, TEXT,
                          WEBSITE, _cover, _font, _hex, _tracked, _tracked_width, _wrap)

VIDEO_SIZES: Dict[str, Tuple[int, int]] = {"9:16": (720, 1280), "4:5": (768, 960), "1:1": (864, 864)}
FPS = 30
DURATION = 10.0
END_CARD_AT = 7.6


# ---------------------------------------------------------------- easing helpers
def _clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _progress(t: float, start: float, length: float) -> float:
    return _clamp((t - start) / length)


def _ease_out(p: float) -> float:
    return 1 - (1 - p) ** 3


def _ease_back(p: float) -> float:
    c = 1.70158
    return 1 + (c + 1) * (p - 1) ** 3 + c * (p - 1) ** 2


def _with_alpha(layer: Image.Image, factor: float) -> Image.Image:
    if factor >= 0.999:
        return layer
    faded = layer.copy()
    faded.putalpha(layer.getchannel("A").point(lambda v: int(v * factor)))
    return faded


def _paste(frame: Image.Image, layer: Image.Image, xy: Tuple[float, float], alpha: float = 1.0) -> None:
    if alpha <= 0.001:
        return
    frame.alpha_composite(_with_alpha(layer, alpha), (int(xy[0]), int(xy[1])))


# ---------------------------------------------------------------- layer builders
def _text_layer(text: str, font, fill, max_w: Optional[int] = None, shadow: bool = False) -> Image.Image:
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    pad = 8 if shadow else 0
    width = int(probe.textlength(text, font=font)) + 4 + 2 * pad
    if max_w:
        width = min(width, max_w + 2 * pad)
    layer = Image.new("RGBA", (width, int(font.size * 1.3) + 2 * pad), (0, 0, 0, 0))
    if shadow:  # soft dark halo keeps white text readable over busy pictures
        halo = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        ImageDraw.Draw(halo).text((pad + 1, pad + 2), text, font=font, fill=(0, 0, 0, 200))
        layer = Image.alpha_composite(layer, halo.filter(ImageFilter.GaussianBlur(4)))
    ImageDraw.Draw(layer).text((pad, pad), text, font=font, fill=fill)
    return layer


def _pill(text: str, font, bg, fg, tracking: float = 0.1) -> Image.Image:
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    pad = int(font.size * 0.8)
    w = _tracked_width(probe, text, font, font.size * tracking) + pad * 2
    h = int(font.size * 2.1)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=bg)
    _tracked(draw, (pad, int(h * 0.22)), text, font, fg, font.size * tracking)
    return layer


def _chip(text: str, font, theme: Dict[str, str]) -> Image.Image:
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    pad_x, pad_y = int(font.size * 0.8), int(font.size * 0.45)
    mark = int(font.size * 0.95)
    w = int(probe.textlength(text, font=font)) + pad_x * 2 + mark + int(font.size * 0.4)
    h = int(font.size * 1.25) + pad_y * 2
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=_hex("#FFFFFF", 240))
    mx, my = pad_x, (h - mark) // 2
    draw.ellipse((mx, my, mx + mark, my + mark), fill=_hex(ACCENT))
    draw.line([(mx + mark * 0.28, my + mark * 0.52), (mx + mark * 0.44, my + mark * 0.68),
               (mx + mark * 0.73, my + mark * 0.34)], fill=_hex(TEXT), width=max(2, mark // 7))
    draw.text((mx + mark + int(font.size * 0.4), pad_y - int(font.size * 0.02)), text, font=font, fill=_hex(NAVY))
    return layer


def _sticker(text: str, radius: int, theme: Dict[str, str]) -> Image.Image:
    size = radius * 2 + 8
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.ellipse((4, 4, size - 4, size - 4), fill=_hex(theme["sticker"]), outline=_hex(TEXT), width=max(3, radius // 18))
    words = text.split()
    half = max(1, len(words) // 2)
    lines = [" ".join(words[:half]), " ".join(words[half:])] if len(words) > 1 else [text]
    lines = [line for line in lines if line]
    size_pt = radius
    while size_pt > 10:
        font = _font("sans", size_pt, 850)
        if max(draw.textlength(l, font=font) for l in lines) <= radius * 1.45 and size_pt * 1.05 * len(lines) <= radius * 1.25:
            break
        size_pt -= 2
    font = _font("sans", size_pt, 850)
    y = (size - size_pt * 1.05 * len(lines)) / 2 - size_pt * 0.08
    for line in lines:
        draw.text(((size - draw.textlength(line, font=font)) / 2, y), line, font=font, fill=_hex(theme["sticker_text"]))
        y += size_pt * 1.05
    return layer.rotate(-10, resample=Image.BICUBIC)


def _logo(width: int, tone: str) -> Image.Image:
    logo = Image.open(os.path.join(ASSETS_DIR, LOGO_FILES[tone])).convert("RGBA")
    return logo.resize((width, int(logo.height * width / logo.width)), Image.LANCZOS)


def _gradient_overlay(size: Tuple[int, int], text_top: Optional[int] = None) -> Image.Image:
    """Dark top band (logo legibility) and deep navy behind the text block, wherever it starts."""
    w, h = size
    text_top = int(h * 0.55) if text_top is None else text_top
    ramp_start, ramp_len = text_top - h * 0.16, h * 0.16
    alpha = Image.new("L", (1, h))
    r, g, b, _ = _hex(NAVY)
    for y in range(h):
        top = 150 * max(0.0, 1 - y / (h * 0.2))
        bottom = 235 * _clamp((y - ramp_start) / ramp_len) ** 1.3
        alpha.putpixel((0, y), int(max(top, bottom)))
    alpha = alpha.resize((w, h))
    overlay = Image.new("RGBA", (w, h), (r, g, b, 0))
    overlay.putalpha(alpha)
    return overlay


# ---------------------------------------------------------------- the video
def render_video(picture: Image.Image, copy: Dict[str, Any], out_path: str, theme_name: str = "solar",
                 ratio: str = "9:16", duration: float = DURATION, fps: int = FPS, bitrate: str = "1400k") -> str:
    """Render the motion ad to an H.264 MP4 at out_path and return the path."""
    import imageio_ffmpeg

    width, height = VIDEO_SIZES.get(ratio, VIDEO_SIZES["9:16"])
    theme = POSTER_THEMES.get(theme_name, POSTER_THEMES["solar"])
    m = int(width * 0.07)
    text_w = width - 2 * m
    headline = str(copy.get("headline") or "").strip()
    subline = str(copy.get("subline") or "").strip()
    eyebrow = str(copy.get("eyebrow") or "").strip().upper()
    sticker = str(copy.get("sticker") or "").strip().upper()[:14]
    cta = str(copy.get("cta") or "Learn More").strip()
    points = [str(p).strip() for p in (copy.get("points") or []) if str(p).strip()][:3]

    # Background: picture cover-cropped 18% larger than the frame, for the zoom.
    zoom_max = 1.18
    big = _cover(picture, (int(width * zoom_max), int(height * zoom_max)))

    # Text layers, rendered once.
    logo = _logo(int(width * 0.26), "dark")
    head_size = int(width * 0.083)
    while head_size > 26:
        head_font = _font("serif", head_size, 800)
        head_lines = _wrap(ImageDraw.Draw(Image.new("RGBA", (1, 1))), headline, head_font, text_w)
        if len(head_lines) <= 4:
            break
        head_size -= 2
    head_layers = [_text_layer(line, head_font, _hex(TEXT), shadow=True) for line in head_lines]
    sub_font = _font("sans", max(16, int(head_size * 0.38)), 500)
    sub_lines = _wrap(ImageDraw.Draw(Image.new("RGBA", (1, 1))), subline, sub_font, text_w)[:3]
    sub_layers = [_text_layer(line, sub_font, _hex("#DCE3F0"), shadow=True) for line in sub_lines]
    pill = _pill(eyebrow, _font("sans", max(16, int(width * 0.032)), 750), _hex(theme["bg"]),
                 _hex(theme["ink"])) if eyebrow else None
    chip_font = _font("sans", max(14, int(width * 0.026)), 600)
    chips = [_chip(p, chip_font, theme) for p in points]
    sticker_layer = _sticker(sticker, int(width * 0.12), theme) if sticker else None

    # Vertical layout of the main scene, bottom-up so the text sits in the navy half.
    head_h = int(head_size * 1.05)
    sub_h = int(sub_font.size * 1.32)
    chip_gap = int(chip_font.size * 0.5)
    chips_h = sum(c.height + chip_gap for c in chips)
    block = (len(head_layers) * head_h + int(head_size * 0.35) + len(sub_layers) * sub_h
             + int(head_size * 0.45) + chips_h)
    # Stories/Reels: the app's reply bar covers the bottom ~20%, so text ends above it.
    text_bottom = height * (0.80 if ratio == "9:16" else 0.94)
    y0 = max(int(height * 0.38), int(text_bottom - block))
    head_y = [y0 + i * head_h for i in range(len(head_layers))]
    sub_y0 = y0 + len(head_layers) * head_h + int(head_size * 0.35)
    chip_y0 = sub_y0 + len(sub_layers) * sub_h + int(head_size * 0.45)
    overlay = _gradient_overlay((width, height), y0)
    shadow_pad = 8  # text layers with a halo carry this padding around the glyphs

    # End card layers.
    card_bg = _hex(theme["bg"])
    card_logo = _logo(int(width * 0.46), theme["logo"])
    card_site = _text_layer(SITE_LINE, _font("sans", max(13, int(width * 0.026)), 600), _hex(theme["ink"]))
    card_head_font = _font("serif", int(width * 0.062), 800)
    card_head = [_text_layer(l, card_head_font, _hex(theme["ink"]))
                 for l in _wrap(ImageDraw.Draw(Image.new("RGBA", (1, 1))), headline, card_head_font, text_w)[:3]]
    cta_font = _font("sans", max(16, int(width * 0.04)), 700)
    cta_label = cta + "  →"
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    cta_w = int(probe.textlength(cta_label, font=cta_font)) + int(cta_font.size * 2.4)
    cta_h = int(cta_font.size * 2.5)
    cta_btn = Image.new("RGBA", (cta_w, cta_h), (0, 0, 0, 0))
    ImageDraw.Draw(cta_btn).rounded_rectangle((0, 0, cta_w - 1, cta_h - 1), radius=cta_h // 2, fill=_hex(ACCENT))
    ImageDraw.Draw(cta_btn).text((int(cta_font.size * 1.2), int((cta_h - cta_font.size * 1.2) / 2)), cta_label,
                                 font=cta_font, fill=_hex(TEXT))
    card_web = _text_layer(WEBSITE, _font("sans", max(15, int(width * 0.034)), 700), _hex(theme["ink"]))
    card_inst = _text_layer(FOOTER_LINE, _font("sans", max(12, int(width * 0.025)), 500), _hex(theme["ink"]))
    # Centre the end card's content in the visible area (above the reply bar for Stories).
    card_total = (card_logo.height + int(width * 0.025) + card_site.height + int(width * 0.09)
                  + len(card_head) * int(card_head_font.size * 1.08) + int(width * 0.07) + cta_h
                  + int(width * 0.08) + card_web.height + int(width * 0.012) + card_inst.height)
    visible = height * (0.85 if ratio == "9:16" else 1.0)
    card_top = int(max(height * 0.06, (visible - card_total) / 2))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    writer = imageio_ffmpeg.write_frames(
        out_path, (width, height), fps=fps, codec="libx264", pix_fmt_in="rgb24", pix_fmt_out="yuv420p",
        bitrate=bitrate, macro_block_size=8, output_params=["-movflags", "+faststart"])
    writer.send(None)
    frames = int(duration * fps)
    for i in range(frames):
        t = i / fps
        # Slow zoom in with a gentle drift.
        z = 1.0 + (zoom_max - 1.0) * (1 - (1 - t / duration) ** 1.6) * 0.85
        cw, ch = big.width / z, big.height / z
        cx = (big.width - cw) / 2 + math.sin(t * 0.35) * big.width * 0.012
        cy = (big.height - ch) / 2
        frame = big.crop((int(cx), int(cy), int(cx + cw), int(cy + ch))).resize((width, height), Image.BILINEAR)
        frame = frame.convert("RGBA")
        frame.alpha_composite(overlay)

        _paste(frame, logo, (m, m), _ease_out(_progress(t, 0.2, 0.6)))
        if pill is not None:
            p = _ease_out(_progress(t, 0.5, 0.6))
            _paste(frame, pill, (m - (1 - p) * (pill.width + m), m + logo.height + int(m * 0.5)), p)
        if sticker_layer is not None:
            p = _progress(t, 0.9, 0.5)
            if p > 0:
                s = max(0.05, _ease_back(p))
                sized = sticker_layer.resize((max(1, int(sticker_layer.width * s)), max(1, int(sticker_layer.height * s))),
                                             Image.BILINEAR)
                cxs, cys = width - m - sticker_layer.width // 2, int(height * 0.24)
                _paste(frame, sized, (cxs - sized.width / 2, cys - sized.height / 2), min(1.0, p * 2))
        for k, (layer, y) in enumerate(zip(head_layers, head_y)):
            p = _ease_out(_progress(t, 1.3 + k * 0.32, 0.5))
            _paste(frame, layer, (m - shadow_pad, y - shadow_pad + (1 - p) * 28), p)
        sub_start = 1.3 + len(head_layers) * 0.32 + 0.3
        for k, layer in enumerate(sub_layers):
            p = _ease_out(_progress(t, sub_start + k * 0.12, 0.5))
            _paste(frame, layer, (m - shadow_pad, sub_y0 + k * sub_h - shadow_pad + (1 - p) * 16), p)
        y = chip_y0
        for k, chip in enumerate(chips):
            p = _ease_out(_progress(t, 4.4 + k * 0.5, 0.45))
            _paste(frame, chip, (m - (1 - p) * 60, y), p)
            y += chip.height + chip_gap

        # End card slides up and covers the frame.
        p = _ease_out(_progress(t, END_CARD_AT, 0.55))
        if p > 0:
            top = int(height * (1 - p))
            card = Image.new("RGBA", (width, height - top), card_bg)
            frame.alpha_composite(card, (0, top))
            if p >= 1:
                q = _ease_out(_progress(t, END_CARD_AT + 0.45, 0.5))
                yy = card_top
                _paste(frame, card_logo, ((width - card_logo.width) / 2, yy + (1 - q) * 20), q)
                yy += card_logo.height + int(width * 0.025)
                _paste(frame, card_site, ((width - card_site.width) / 2, yy), q)
                yy += card_site.height + int(width * 0.09)
                q2 = _ease_out(_progress(t, END_CARD_AT + 0.75, 0.5))
                for layer in card_head:
                    _paste(frame, layer, ((width - layer.width) / 2, yy + (1 - q2) * 18), q2)
                    yy += int(card_head_font.size * 1.08)
                yy += int(width * 0.07)
                q3 = _progress(t, END_CARD_AT + 1.05, 0.4)
                pulse = 1 + 0.035 * math.sin(max(0.0, t - END_CARD_AT - 1.5) * 5.0)
                btn = cta_btn if q3 < 1 else cta_btn.resize((int(cta_w * pulse), int(cta_h * pulse)), Image.BILINEAR)
                _paste(frame, btn, ((width - btn.width) / 2, yy - (btn.height - cta_h) / 2), _ease_out(q3))
                yy += cta_h + int(width * 0.08)
                _paste(frame, card_web, ((width - card_web.width) / 2, yy), _ease_out(q3))
                yy += card_web.height + int(width * 0.012)
                _paste(frame, card_inst, ((width - card_inst.width) / 2, yy), _ease_out(q3))
        writer.send(frame.convert("RGB").tobytes())
    writer.close()
    return out_path


def main() -> int:
    """Render a demo video from the first showcase ad in web_data/site_data.json."""
    import json
    import sys
    import time

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data = json.load(open(os.path.join(root, "web_data", "site_data.json"), encoding="utf-8"))
    item = data["showcase"][int(sys.argv[1]) if len(sys.argv) > 1 else 0]
    picture = Image.open(os.path.join(root, item["picture"].lstrip("/")))
    out = os.path.join(root, "data", "output", "videos", item["trend"]["trend_id"] + "_9x16.mp4")
    start = time.time()
    render_video(picture, item["copy"], out, item["theme"], "9:16")
    print("Rendered {} in {:.1f}s ({:.1f} MB)".format(out, time.time() - start, os.path.getsize(out) / 1e6))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
