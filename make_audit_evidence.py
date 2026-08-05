"""Build the YouTube API audit's conditional evidence PDF (item b).

The audit asks for "Upload Interface Screenshots". This API Client has no
user-facing upload UI — it is a scheduled backend pipeline that uploads one
video a day to the operator's own channel — so the honest evidence is the
interface that actually exists: the trigger surface, a real execution log, the
videos.insert implementation, and the artifact it produced.

Renders A4 pages at 150 DPI into a single PDF.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import matplotlib

BASE = Path(__file__).resolve().parent
OUT = BASE / "build" / "upload-interface-evidence.pdf"

W, H = 1240, 1754                      # A4 @ 150dpi
M = 90                                 # margin
INK = (17, 24, 39)
MUTED = (100, 116, 139)
ACCENT = (37, 99, 235)
RULE = (203, 213, 225)
CODE_BG = (246, 248, 250)
GREEN = (22, 163, 74)

_FONTS = Path(matplotlib.get_data_path()) / "fonts" / "ttf"


def font(size: int, bold: bool = False, mono: bool = False):
    if mono:
        name = "DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"
    else:
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(_FONTS / name), size)


def page() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), "white")
    return img, ImageDraw.Draw(img)


def header(d, title: str, n: int):
    d.text((M, 66), "QuantAura Channel Pipeline", font=font(22, True), fill=ACCENT)
    d.text((W - M, 66), f"Upload Interface Evidence · p{n}", font=font(18),
           fill=MUTED, anchor="ra")
    d.line([(M, 104), (W - M, 104)], fill=RULE, width=2)
    d.text((M, 140), title, font=font(30, True), fill=INK)
    return 200


def para(d, y: int, text: str, size: int = 19, fill=INK, width: int = 96,
         leading: int = 30) -> int:
    f = font(size)
    for raw in text.split("\n"):
        if not raw.strip():
            y += leading // 2
            continue
        line = ""
        for word in raw.split():
            trial = f"{line} {word}".strip()
            if len(trial) <= width:
                line = trial
            else:
                d.text((M, y), line, font=f, fill=fill)
                y += leading
                line = word
        if line:
            d.text((M, y), line, font=f, fill=fill)
            y += leading
    return y


def code_block(d, y: int, lines: list[str], size: int = 15,
               highlight: tuple[str, ...] = ()) -> int:
    f = font(size, mono=True)
    fb = font(size, mono=True, bold=True)
    pad, lh = 22, int(size * 1.55)
    box_h = len(lines) * lh + pad * 2
    d.rounded_rectangle([M, y, W - M, y + box_h], radius=10, fill=CODE_BG,
                        outline=RULE, width=1)
    ty = y + pad
    for ln in lines:
        hit = any(h in ln for h in highlight)
        d.text((M + pad, ty), ln, font=fb if hit else f,
               fill=GREEN if hit else INK)
        ty += lh
    return y + box_h + 26


def wrap_mono(lines: list[str], width: int = 92) -> list[str]:
    """Fold over-long log lines instead of clipping them — a silently truncated
    line in an audit exhibit is a misrepresentation, however small."""
    out: list[str] = []
    for ln in lines:
        while len(ln) > width:
            cut = ln.rfind(" ", 0, width)
            if cut < width // 2:
                cut = width
            out.append(ln[:cut])
            ln = "    " + ln[cut:].lstrip()
        out.append(ln)
    return out


def label(d, y: int, text: str) -> int:
    d.text((M, y), text.upper(), font=font(16, True), fill=MUTED)
    return y + 34


# ── Page 1: overview ─────────────────────────────────────────────────────────

def p1():
    img, d = page()
    y = header(d, "1 · What the API Client is", 1)
    y = para(d, y,
             "QuantAura Channel Pipeline is a scheduled backend service. It publishes one "
             "short video per day to a single YouTube channel owned by the applicant "
             "(@quantaura_ml). Each video reports that day's machine-learning model "
             "prediction for Bitcoin, a chart of real market data, and the model's own "
             "running accuracy record including its incorrect calls.")
    y += 16
    y = para(d, y,
             "There is no user-facing upload interface, and no end user ever signs in. The "
             "pipeline authenticates with the applicant's own OAuth refresh token, held as "
             "an encrypted CI secret, and writes only to the applicant's own channel. It "
             "reads no data belonging to any other YouTube user or channel.")
    y += 28

    y = label(d, y, "Endpoints used")
    y = code_block(d, y, [
        "youtube.videos.insert      1 call/day    1,600 units   upload the daily video",
        "youtube.thumbnails.set     1 call/day       50 units   set its thumbnail",
        "youtube.channels.list      1 call/day        1 unit    confirm upload destination",
        "",
        "Total ~1,651 units/day against the default 10,000-unit quota.",
    ])

    y = label(d, y, "Daily sequence")
    steps = [
        ("1", "Fetch", "model prediction + market data from quantaura.tech"),
        ("2", "Grade", "score the previous call against the realised 24h price move"),
        ("3", "Script", "generate narration from the day's figures"),
        ("4", "Verify", "reject the script if any figure is unsupported"),
        ("5", "Render", "synthesise narration, draw charts, encode MP4"),
        ("6", "Upload", "videos.insert → thumbnails.set (private by default)"),
        ("7", "Notify", "report the outcome to the operator"),
    ]
    bx, bw, bh, gap = M, W - 2 * M, 62, 12
    for num, name, desc in steps:
        d.rounded_rectangle([bx, y, bx + bw, y + bh], radius=8,
                            fill=(250, 251, 253), outline=RULE, width=1)
        d.ellipse([bx + 18, y + 17, bx + 46, y + 45], fill=ACCENT)
        d.text((bx + 32, y + 31), num, font=font(16, True), fill="white", anchor="mm")
        d.text((bx + 62, y + 31), name, font=font(19, True), fill=INK, anchor="lm")
        d.text((bx + 200, y + 31), desc, font=font(17), fill=MUTED, anchor="lm")
        y += bh + gap
    return img


# ── Page 2: trigger surface ──────────────────────────────────────────────────

def p2():
    img, d = page()
    y = header(d, "2 · Upload trigger interface", 2)
    y = para(d, y,
             "Uploads are initiated in exactly two ways: an automatic daily schedule, and a "
             "manual operator trigger in GitHub Actions. Both are shown below, taken from "
             "the deployed workflow definition. No other path can invoke an upload.")
    y += 20

    y = label(d, y, "Deployed workflow — .github/workflows/daily-video.yml")
    y = code_block(d, y, [
        "on:",
        "  schedule:",
        "    - cron: '0 13 * * *'          # one run per day, 13:00 UTC",
        "  workflow_dispatch:               # operator-triggered run",
        "    inputs:",
        "      no_upload:",
        "        description: 'Build the video but do not publish it'",
        "        type: boolean",
        "        default: false",
        "      no_voice:",
        "        description: 'Silent placeholder audio (fast render check)'",
        "        type: boolean",
        "        default: false",
        "      dry_run:",
        "        description: 'Synthetic data, no API calls, no upload'",
        "        type: boolean",
        "        default: false",
        "",
        "concurrency:",
        "  group: daily-video               # two runs can never upload at once",
        "  cancel-in-progress: false",
    ], highlight=("cron:", "workflow_dispatch:"))

    y = label(d, y, "Credentials")
    y = code_block(d, y, [
        "YT_CLIENT_ID       ]",
        "YT_CLIENT_SECRET   ]  encrypted repository secrets, injected at runtime",
        "YT_REFRESH_TOKEN   ]  never written to disk, never logged",
        "",
        "Granted scopes:",
        "  https://www.googleapis.com/auth/youtube.upload",
        "  https://www.googleapis.com/auth/youtube.readonly",
    ])

    y += 6
    y = para(d, y,
             "The readonly scope is used solely to call channels.list(mine=true) and confirm "
             "the upload is going to the applicant's own channel before any video is sent.",
             size=17, fill=MUTED)
    return img


# ── Page 3: real execution log ───────────────────────────────────────────────

def p3():
    img, d = page()
    y = header(d, "3 · Upload execution — real run log", 3)
    y = para(d, y,
             "Output of a production run on the GitHub Actions runner. The destination "
             "channel is logged before the upload begins, so a misconfigured credential is "
             "visible in the record rather than discovered afterwards.")
    y += 20

    # The log is read from a captured file, never composed here. This page is
    # submitted under a truthfulness attestation, so a plausible-looking
    # transcript that no run actually produced is not an option.
    log_path = BASE / "build" / "upload_run.log"
    if not log_path.exists():
        raise SystemExit(
            f"MISSING: {log_path}\n"
            "Page 3 must show a genuine upload run. Trigger the workflow with all\n"
            "inputs false, then save the 'Run pipeline' step's log to that path and\n"
            "re-run this script."
        )
    raw = [ln.rstrip() for ln in log_path.read_text(encoding="utf-8",
                                                    errors="replace").splitlines()]
    # Keep only this program's own log lines, stripped of their timestamp and
    # level prefix. Runner infrastructure noise is not evidence.
    cleaned = []
    for ln in raw:
        if "] channel: " not in ln:
            continue
        body = ln.split("] channel: ", 1)[1]
        if body.strip():
            cleaned.append(body)

    y = label(d, y, "Runner output — captured from a live production run")
    y = code_block(d, y, wrap_mono(cleaned),
                   highlight=("Channel:", "Uploading to YouTube", "Uploaded:"))

    y += 4
    y = para(d, y,
             "Every run - success, skipped day, or failure - is recorded to an append-only "
             "log committed to the repository, and reported to the operator. Uploads are "
             "created as private and reviewed by the operator before any change of "
             "visibility.", size=17, fill=MUTED)
    return img


# ── Page 4: implementation ───────────────────────────────────────────────────

def p4():
    img, d = page()
    y = header(d, "4 · Upload implementation", 4)
    y = para(d, y,
             "The complete videos.insert request body, from channel/upload.py in the "
             "applicant's repository. Policy-relevant fields are highlighted.")
    y += 20

    y = label(d, y, "channel/upload.py — request body")
    y = code_block(d, y, [
        "body = {",
        "    \"snippet\": {",
        "        \"title\":       script[\"title\"],",
        "        \"description\": build_description(script, snapshot, score),",
        "        \"tags\":        script[\"tags\"],",
        "        \"categoryId\":  \"25\",",
        "        \"defaultLanguage\": \"en\",",
        "    },",
        "    \"status\": {",
        "        \"privacyStatus\":           \"private\",",
        "        \"selfDeclaredMadeForKids\": False,",
        "        \"containsSyntheticMedia\":  True,",
        "        \"license\":                 \"youtube\",",
        "        \"embeddable\":              True,",
        "    },",
        "}",
        "",
        "request = service.videos().insert(",
        "    part=\"snippet,status\", body=body, media_body=media)",
    ], highlight=("privacyStatus", "containsSyntheticMedia", "selfDeclaredMadeForKids"))

    y = label(d, y, "Disclosure text appended to every description, in code")
    y = code_block(d, y, [
        "This video was generated automatically. The narration voice is",
        "synthetic and the script was written by an AI model from the",
        "ensemble's real output.",
        "",
        "NOT FINANCIAL ADVICE. Nothing here is a recommendation to buy or",
        "sell any asset. The model is frequently wrong; that is exactly why",
        "the scoreboard is public. Do your own research.",
    ], size=15)

    y += 4
    y = para(d, y,
             "The disclosure is concatenated by the program and cannot be omitted or altered "
             "by the language model that drafts the script. containsSyntheticMedia is set on "
             "every upload rather than left to a manual setting.", size=17, fill=MUTED)
    return img


# ── Page 5: the artifact ─────────────────────────────────────────────────────

def p5():
    img, d = page()
    y = header(d, "5 · Uploaded artifact", 5)
    y = para(d, y,
             "A video and thumbnail produced by the pipeline, in the form they are uploaded. "
             "This is a different day's run from the log on page 3; every day's output has "
             "the same structure and differs only in its figures. All values on screen are "
             "drawn from live market data, and no part of the frame is generatively produced.")
    y += 24

    frame = BASE / "build" / "evidence_frame.png"
    thumb = BASE / "build" / "thumbnail.jpg"

    fh = 700
    if frame.exists():
        im = Image.open(frame).convert("RGB")
        fw = int(im.width * fh / im.height)
        im = im.resize((fw, fh), Image.LANCZOS)
        img.paste(im, (M, y))
        d.rectangle([M, y, M + fw, y + fh], outline=RULE, width=1)
        d.text((M, y + fh + 12), "Video frame · 1080x1920 · 58.8s",
               font=font(16), fill=MUTED)
        tx = M + fw + 40
    else:
        tx = M

    if thumb.exists():
        im = Image.open(thumb).convert("RGB")
        tw = W - M - tx
        th = int(im.height * tw / im.width)
        im = im.resize((tw, th), Image.LANCZOS)
        img.paste(im, (tx, y))
        d.rectangle([tx, y, tx + tw, y + th], outline=RULE, width=1)
        d.text((tx, y + th + 12), "Thumbnail · thumbnails.set", font=font(16), fill=MUTED)

    y += fh + 70
    y = label(d, y, "Compliance summary")
    for line in [
        "Uploads only to the applicant's own channel (@quantaura_ml).",
        "No data read, stored or processed from any other YouTube user or channel.",
        "One upload per day; ~1,651 quota units, within the default allowance.",
        "Every video created private and reviewed before publication.",
        "Synthetic-content disclosure set via the API on every upload.",
        "Automated generation and 'not financial advice' stated in every description.",
        "Privacy policy: https://quantaura.tech/privacy",
        "Terms of service: https://quantaura.tech/terms",
    ]:
        d.ellipse([M + 4, y + 9, M + 14, y + 19], fill=GREEN)
        d.text((M + 30, y), line, font=font(18), fill=INK)
        y += 34
    return img


def main() -> None:
    pages = [p1(), p2(), p3(), p4(), p5()]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(OUT, "PDF", resolution=150.0, save_all=True, append_images=pages[1:])
    size_kb = OUT.stat().st_size / 1024
    print(f"{OUT}  ({len(pages)} pages, {size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
