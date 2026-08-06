"""Thumbnail generation.

The daily text is stamped with Pillow rather than generated, because an image
model cannot be relied on to render "HOLD · 41%" correctly and a wrong number
on a thumbnail is a wrong number in every feed that shows it. Any AI-generated
art is used as a *background* only, refreshed weekly, with the text drawn on
top deterministically.

Note: YouTube does not show custom thumbnails on Shorts — this matters for the
weekly long-form recap, and is generated daily anyway because it costs
milliseconds and makes the archive browsable.
"""
from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from common import ASSETS_DIR, BUILD_DIR, config, log

TW, TH = 1280, 720


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """matplotlib ships DejaVu, so no font file has to be vendored or licensed."""
    import matplotlib
    base = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(str(base / name), size)
    except Exception:                                            # noqa: BLE001
        return ImageFont.load_default()


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))          # type: ignore[return-value]


def _background() -> Image.Image:
    """Newest weekly AI background if one exists, else a generated gradient."""
    t = config()["theme"]
    candidates = sorted(ASSETS_DIR.glob("thumb_bg_*.png"))
    if candidates:
        try:
            img = Image.open(candidates[-1]).convert("RGB").resize((TW, TH))
            # Darken so white text always clears contrast, whatever was generated.
            overlay = Image.new("RGB", (TW, TH), _hex(t["bg"]))
            return Image.blend(img, overlay, 0.55)
        except Exception as e:                                   # noqa: BLE001
            log.warning(f"  Thumbnail background unusable ({e}) — using gradient")

    img = Image.new("RGB", (TW, TH), _hex(t["bg"]))
    d = ImageDraw.Draw(img)
    accent = _hex(t["accent"])
    for i in range(TH):
        k = i / TH
        d.line([(0, i), (TW, i)],
               fill=tuple(int(_hex(t["bg"])[c] + (accent[c] - _hex(t["bg"])[c]) * k * 0.22)
                          for c in range(3)))
    # Faint dot grid, blurred — texture without competing with the text.
    rnd = random.Random(7)
    for _ in range(220):
        x, y = rnd.randint(0, TW), rnd.randint(0, TH)
        d.ellipse([x, y, x + 3, y + 3], fill=_hex(t["grid"]))
    return img.filter(ImageFilter.GaussianBlur(0.6))


def build(snapshot: dict, score: dict, out_name: str = "thumbnail.jpg") -> Path:
    t = config()["theme"]
    img = _background()
    d = ImageDraw.Draw(img)

    sig = snapshot["signal"]
    sig_color = {"BUY": _hex(t["up"]), "SELL": _hex(t["down"])}.get(sig, _hex(t["flat"]))

    # Brand left, date right, nothing between them. A centre label here
    # collided with the brand word at its real rendered width — the same
    # mistake the video header had.
    d.text((64, 52), "QUANTAURA", font=_font(38, True), fill=_hex(t["accent"]))
    d.text((TW - 64, 56), snapshot["date"], font=_font(32), fill=_hex(t["muted"]), anchor="ra")

    d.text((64, 150), "TODAY'S CALL", font=_font(34), fill=_hex(t["muted"]))
    d.text((60, 196), sig, font=_font(190, True), fill=sig_color)

    conf = snapshot.get("confidence")
    if conf is not None:
        d.text((64, 420), f"{conf:.0f}% confidence", font=_font(46), fill=_hex(t["text"]))

    d.text((64, 510), f"{snapshot['asset_label']}  {snapshot['price_str']}",
           font=_font(52, True), fill=_hex(t["text"]))
    chg = snapshot.get("change_24h_pct")
    if chg is not None:
        d.text((64, 580), f"{chg:+.2f}% in 24h", font=_font(40),
               fill=_hex(t["up"] if chg >= 0 else t["down"]))

    # Right column: the running record, in the same place every day so a
    # returning viewer's eye already knows where to look.
    acc = score.get("accuracy_pct")
    resolved = score.get("resolved_calls") or 0
    bx, by, bw, bh = TW - 430, 170, 366, 300
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=26, fill=_hex(t["panel"]))
    d.text((bx + bw / 2, by + 44), "PUBLIC RECORD", font=_font(30), fill=_hex(t["muted"]),
           anchor="ma")
    if resolved:
        d.text((bx + bw / 2, by + 96), f"{acc:.0f}%", font=_font(120, True),
               fill=_hex(t["up"] if acc and acc >= 50 else t["down"]), anchor="ma")
        d.text((bx + bw / 2, by + 236),
               f"{score.get('hits', 0)}W · {score.get('misses', 0)}L "
               f"over {resolved}", font=_font(30), fill=_hex(t["text"]), anchor="ma")
    else:
        d.text((bx + bw / 2, by + 120), "DAY 1", font=_font(96, True),
               fill=_hex(t["accent"]), anchor="ma")
        d.text((bx + bw / 2, by + 236), "record starts now", font=_font(28),
               fill=_hex(t["muted"]), anchor="ma")

    d.text((64, TH - 62), "Automated research project · not financial advice",
           font=_font(28), fill=_hex(t["muted"]))

    out = BUILD_DIR / out_name
    img.save(out, "JPEG", quality=90)
    log.info(f"  Thumbnail: {out.name}")
    return out


def build_weekly(facts: dict, out_name: str = "weekly_thumbnail.jpg") -> Path:
    """Thumbnail for the Sunday recap — the one video type where a custom
    thumbnail is actually shown, so it leads with the week's record."""
    t = config()["theme"]
    img = _background()
    d = ImageDraw.Draw(img)

    d.text((64, 52), "QUANTAURA", font=_font(38, True), fill=_hex(t["accent"]))
    d.text((TW - 64, 56), f"WEEK {facts['week_number']}", font=_font(32),
           fill=_hex(t["muted"]), anchor="ra")

    hits, res = facts["week_hits"], facts["week_resolved"]
    acc = facts.get("week_accuracy_pct")
    good = acc is not None and acc >= 50
    d.text((64, 140), "THIS WEEK", font=_font(34), fill=_hex(t["muted"]))
    d.text((58, 180), f"{hits} OF {res}", font=_font(170, True),
           fill=_hex(t["up"] if good else t["down"]))
    d.text((64, 400), "CALLS CORRECT", font=_font(56, True), fill=_hex(t["text"]))

    # The week's outcomes as oversized dots — legible at feed size, which is
    # the entire job of a thumbnail.
    x, y, dot, gap = 68, 520, 58, 18
    for day in facts["days"]:
        colour = _hex(t["up"] if day["correct"] else t["down"])
        d.ellipse([x, y, x + dot, y + dot], fill=colour)
        mark = "✓" if day["correct"] else "✗"
        d.text((x + dot / 2, y + dot / 2), mark, font=_font(34, True),
               fill=_hex(t["bg"]), anchor="mm")
        x += dot + gap

    alltime = facts.get("alltime_accuracy_pct")
    if alltime is not None:
        bx, by, bw, bh = TW - 420, 190, 356, 260
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=26, fill=_hex(t["panel"]))
        d.text((bx + bw / 2, by + 40), "ALL-TIME", font=_font(30),
               fill=_hex(t["muted"]), anchor="ma")
        d.text((bx + bw / 2, by + 86), f"{alltime:.0f}%", font=_font(110, True),
               fill=_hex(t["up"] if alltime >= 50 else t["down"]), anchor="ma")
        d.text((bx + bw / 2, by + 212),
               f"{facts.get('alltime_resolved', 0)} graded calls",
               font=_font(28), fill=_hex(t["text"]), anchor="ma")

    d.text((64, TH - 62), "Automated research project · not financial advice",
           font=_font(28), fill=_hex(t["muted"]))

    out = BUILD_DIR / out_name
    img.save(out, "JPEG", quality=90)
    log.info(f"  Weekly thumbnail: {out.name}")
    return out
