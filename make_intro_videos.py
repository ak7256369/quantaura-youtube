"""One-off: the channel's three evergreen introduction Shorts.

    python make_intro_videos.py --no-upload   # render only, inspect first
    python make_intro_videos.py               # render + upload (private)

Three videos, each explaining one thing a new viewer needs:
    1. meet-quantaura      — what the project is
    2. the-scoreboard      — how calls are graded, and why that is checkable
    3. inside-the-ensemble — how four models become one call

Deliberate differences from the daily pipeline:
  * No LLM. The scripts are fixed prose written once, because the content is
    evergreen and every claim in it must stay true for the life of the channel.
  * No market data. Nothing here can go stale with the price.
  * Same voice, theme, captions, and publish path as the dailies, so the
    channel looks like one product — publish() carries the same disclosure
    flags, and uploads land private for the operator to schedule in Studio.

The two numbers the narration speaks (the 24-hour horizon and the ±1% flat
band) are asserted against config.yaml at build time: if the grading rule ever
changes, this script refuses to build rather than produce a video that lies
about it.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import BUILD_DIR, config, log                          # noqa: E402
import render as R                                                 # noqa: E402
import voice as V                                                  # noqa: E402


# ── Scene painters (reuse the daily renderer's canvas + theme) ────────────────

def _fit(fig, ax, y: float, s: str, size: int, **kw) -> None:
    """Draw centred text, then measure and shrink until it actually fits.

    Estimating DejaVu's width by character count is how the first render
    clipped "CALLS BITCOIN" off both edges — measure the real extent instead.
    """
    txt = R._text(ax, R.W / 2, y, s, size=size, ha="center", **kw)
    renderer = fig.canvas.get_renderer()
    max_w = R.W - 140
    while size > 40:
        # Extent is in display units; the canvas is drawn at 100 dpi so display
        # pixels and our coordinate pixels coincide.
        w = txt.get_window_extent(renderer=renderer).width
        if w <= max_w:
            break
        size = int(size * max_w / w) - 1
        txt.set_fontsize(size)


def title_card(label: str, big: str, sub: str, path: Path) -> Path:
    t = R._theme()
    fig, ax = R._canvas()
    R._header(ax, {"date": ""}, label)
    words = big.split()
    mid = (len(words) + 1) // 2
    _fit(fig, ax, 640, " ".join(words[:mid]), 110, weight="bold")
    _fit(fig, ax, 790, " ".join(words[mid:]), 110, weight="bold", color=t["accent"])
    for i, line in enumerate(R._wrap(sub, 30)[:3]):
        R._text(ax, R.W / 2, 1010 + i * 70, line, size=44, color=t["muted"], ha="center")
    return R._save(fig, path)


def points_card(label: str, heading: str, points: list[tuple[str, str]], path: Path) -> Path:
    t = R._theme()
    fig, ax = R._canvas()
    R._header(ax, {"date": ""}, label)
    R._text(ax, R.W / 2, 330, heading, size=56, weight="bold", ha="center")
    y = 460
    for name, role in points:
        R._panel(ax, 96, y, R.W - 192, 150, color=t["panel"])
        ax.plot([136, 136], [y + 40, y + 110], color=t["accent"], lw=8,
                solid_capstyle="round", zorder=3)
        R._text(ax, 176, y + 56, name, size=42, weight="bold")
        R._text(ax, 176, y + 108, role, size=32, color=t["muted"])
        y += 178
    return R._save(fig, path)


def statement_card(label: str, lines: list[str], sub: str, path: Path) -> Path:
    t = R._theme()
    fig, ax = R._canvas()
    R._header(ax, {"date": ""}, label)
    y = 620
    for line in lines:
        _fit(fig, ax, y, line, 92, weight="bold",
             color=t["accent"] if line is lines[-1] else t["text"])
        y += 130
    for i, ln in enumerate(R._wrap(sub, 32)[:4]):
        R._text(ax, R.W / 2, y + 90 + i * 66, ln, size=40, color=t["muted"], ha="center")
    return R._save(fig, path)


# ── The three videos ──────────────────────────────────────────────────────────

def video_specs() -> list[dict]:
    cfg = config()
    horizon = cfg["scoring"]["horizon_hours"]
    band = cfg["scoring"]["flat_band_pct"]
    # The narration below says these numbers in words. If the rule changes,
    # fail here — never ship a video that misstates the grading.
    assert horizon == 24, f"narration says 'twenty four hours'; config says {horizon}"
    assert band == 1.0, f"narration says 'one percent'; config says {band}"

    return [
        {
            "slug": "meet-quantaura",
            "title": "Meet QuantAura — a model calls Bitcoin, in public",
            "tags": ["bitcoin", "machine learning", "crypto", "quantaura",
                     "algorithmic trading", "data science", "ai"],
            "description": (
                "QuantAura is a university research project: four machine-learning "
                "models vote on Bitcoin's next 24 hours, every day, and every call is "
                "graded in public — wins and losses alike.\n\n"
                "New call every day. Full research at https://quantaura.tech"
            ),
            "scenes": [
                ("hook", title_card, dict(
                    label="about this channel",
                    big="THE MODEL CALLS BITCOIN",
                    sub="every day · in public")),
                ("models", points_card, dict(
                    label="the ensemble",
                    heading="Four models vote",
                    points=[("LSTM", "reads price sequences"),
                            ("Transformer", "weighs what matters"),
                            ("XGBoost", "handles engineered features"),
                            ("KAN", "searches for formulas")])),
                ("promise", statement_card, dict(
                    label="the difference",
                    lines=["EVERY CALL", "GOES PUBLIC"],
                    sub="logged before the outcome is knowable, graded after")),
            ],
            "narration": {
                "hook": ["Every day, four machine learning models vote on where "
                         "Bitcoin is heading over the next twenty four hours."],
                "models": ["An L S T M, a transformer, a gradient boosted model, "
                           "and a K A N.",
                           "Their votes blend into a single call. Buy, hold, or "
                           "sell, with a confidence score."],
                "promise": ["The call is published before the outcome is knowable, "
                            "then graded in public. Wins and losses alike."],
            },
        },
        {
            "slug": "the-scoreboard",
            "title": "The Scoreboard — how every QuantAura call is graded",
            "tags": ["bitcoin", "machine learning", "accuracy", "quantaura",
                     "crypto", "backtest", "transparency"],
            "description": (
                "How the QuantAura scoreboard works: every daily Bitcoin call is "
                "logged before the outcome is knowable, then graded 24 hours later "
                "on the realised price move, with moves inside ±1% counted as flat. "
                "Misses stay on the record.\n\n"
                "New call every day. Full research at https://quantaura.tech"
            ),
            "scenes": [
                ("hook", title_card, dict(
                    label="about the scoreboard",
                    big="THE WHOLE RECORD",
                    sub="not just the wins")),
                ("rules", points_card, dict(
                    label="the grading rule",
                    heading="Four rules, no exceptions",
                    points=[("Logged first", "before the outcome is knowable"),
                            ("Graded at 24h", "on the realised price move"),
                            ("Flat band ±1%", "small moves count as flat"),
                            ("Misses stay", "the record is never edited")])),
                ("check", statement_card, dict(
                    label="why it is different",
                    lines=["CHECK EVERY", "NUMBER"],
                    sub="graded on plain price moves you can verify on any chart")),
            ],
            "narration": {
                "hook": ["Most prediction accounts show you their wins. This "
                         "channel shows you the whole record."],
                "rules": ["Every call is written down before the outcome is "
                          "knowable, and graded twenty four hours later on the "
                          "real price move.",
                          "Moves inside a one percent band count as flat. A miss "
                          "stays on the record forever."],
                "check": ["The grading uses plain price changes you can verify on "
                          "any chart. No hidden metrics, and no quiet edits."],
            },
        },
        {
            "slug": "inside-the-ensemble",
            "title": "Inside the Ensemble — how four models become one call",
            "tags": ["machine learning", "ensemble", "lstm", "transformer",
                     "xgboost", "kan", "bitcoin", "quantaura"],
            "description": (
                "Inside QuantAura's ensemble: an LSTM, a Transformer, an XGBoost "
                "model and a KAN each read the Bitcoin market differently. Their "
                "votes are blended with learned weights, and a confidence gate "
                "downgrades low-confidence calls to HOLD rather than forcing a "
                "guess.\n\n"
                "New call every day. Full research at https://quantaura.tech"
            ),
            "scenes": [
                ("hook", title_card, dict(
                    label="inside the model",
                    big="FOUR MODELS ONE CALL",
                    sub="how the ensemble decides")),
                ("roles", points_card, dict(
                    label="who sees what",
                    heading="Different eyes, same market",
                    points=[("LSTM", "sequences — how we got here"),
                            ("Transformer", "attention — what matters now"),
                            ("XGBoost", "features — the measurable state"),
                            ("KAN", "formulas — the shape of the rule")])),
                ("gate", statement_card, dict(
                    label="the safety valve",
                    lines=["LOW CONFIDENCE", "= HOLD"],
                    sub="the gate would rather say nothing than guess")),
            ],
            "narration": {
                "hook": ["QuantAura is an ensemble. Four model families study the "
                         "same market, and they vote."],
                "roles": ["The L S T M reads price sequences. The transformer "
                          "weighs which moments matter most.",
                          "The gradient boosted model handles engineered market "
                          "features, and the K A N searches for symbolic formulas."],
                "gate": ["The votes are blended with learned weights. When "
                         "confidence is too low, a gate downgrades the call to "
                         "hold rather than forcing a guess."],
            },
        },
    ]


# ── Build ─────────────────────────────────────────────────────────────────────

def build_one(spec: dict) -> Path:
    slug = spec["slug"]
    log.info(f"-- {slug} --")           # ASCII: Windows consoles choke on box chars

    # Voice first: scene durations come from the narration timings.
    sentences = [(sec, s) for sec, _, _ in spec["scenes"]
                 for s in spec["narration"][sec]]
    sentences.append(("outro", config()["disclaimer"]["narration"]))
    narration = V.synthesize(sentences, out_name=f"{slug}.wav")

    gap = config()["voice"]["sentence_gap_seconds"]
    clips: list[Path] = []
    for i, (sec, painter, kwargs) in enumerate(spec["scenes"]):
        png = painter(**kwargs, path=BUILD_DIR / f"{slug}_{sec}.png")
        dur = max(narration.section_duration(sec) + gap, 2.0)
        clips.append(R._still_clip(png, dur, BUILD_DIR / f"{slug}_clip{i}.mp4"))

    outro_png = R.scene_outro({"date": ""}, BUILD_DIR / f"{slug}_outro.png")
    clips.append(R._still_clip(outro_png, max(narration.section_duration("outro"), 2.5),
                               BUILD_DIR / f"{slug}_clip_outro.mp4", drift=False))

    body = R._concat(clips, BUILD_DIR / f"{slug}_silent.mp4")
    ass = R.build_captions(narration, BUILD_DIR / f"{slug}.ass")
    final = BUILD_DIR / f"{slug}.mp4"
    escaped = ass.as_posix().replace(":", r"\:")
    R._run(["-i", str(body), "-i", str(narration.wav_path),
            "-vf", f"subtitles='{escaped}'",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-shortest",
            str(final)], f"{slug} mux")

    dur = R.duration_of(final)
    log.info(f"  {slug}.mp4 ready ({dur:.1f}s)" if dur else f"  {slug}.mp4 ready")
    return final


def main() -> int:
    p = argparse.ArgumentParser(description="Build the three intro Shorts")
    p.add_argument("--no-upload", action="store_true", help="render only")
    p.add_argument("--only", help="build a single slug")
    args = p.parse_args()

    specs = [s for s in video_specs() if not args.only or s["slug"] == args.only]
    if not specs:
        log.error(f"No such slug: {args.only}")
        return 1

    results = []
    for spec in specs:
        video = build_one(spec)
        results.append((spec, video))

    if args.no_upload:
        log.info("Render-only run. Videos in channel/build/ — watch them, then "
                 "re-run without --no-upload.")
        return 0

    import upload as U
    base = config()["disclaimer"]["description"].strip()
    for spec, video in results:
        desc = (spec["description"] + "\n\nThis video was generated automatically. "
                "The narration voice is synthetic.\n" + base)
        vid, url = U.publish(video, None, title=spec["title"],
                             description=desc, tags=spec["tags"])
        log.info(f"  {spec['slug']}: {url}")
    log.info("All intro videos uploaded PRIVATE. Schedule them in YouTube Studio: "
             "Content → video → Visibility → Schedule.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
