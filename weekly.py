"""The Sunday long-form recap: every graded call this week, reviewed on camera.

    python weekly.py                 # build + upload per config.yaml
    python weekly.py --no-upload     # build only
    python weekly.py --no-voice      # silent placeholder audio (layout work)
    python weekly.py --dry-run       # synthetic week, no API calls, no upload

Format: 1920x1080, 3-4 minutes. Six scenes:
    title  → the week's record, huge
    days   → the call-by-call table
    trend  → week price chart with each call marked, hit or miss
    research → one rotating segment on how the model actually works
    watchlist → the other symbols the ensemble covers (CTA, no calls shown)
    outro  → disclaimer

Narrative sections `intro`/`days`/`trend` are LLM-written from the graded rows
and pass the same fact-check gates as the daily video. `research`, `watchlist`
and `outro` are fixed prose written once, because their content must stay true
regardless of what any model returns.

The watchlist deliberately shows WHICH symbols the ensemble covers and never
what it says about them today — those calls are the site's premium feature,
and giving them away daily would unmake the channel's own funnel.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import factcheck                                                   # noqa: E402
import notify                                                      # noqa: E402
import prices                                                      # noqa: E402
import render as R                                                 # noqa: E402
import scoreboard                                                  # noqa: E402
import voice as V                                                  # noqa: E402
from common import (BUILD_DIR, PipelineAbort, config, log,          # noqa: E402
                    published_today, record_run, write_json)
from llm import complete_json                                      # noqa: E402
from common import prompt as load_prompt                           # noqa: E402

SECTIONS = ["intro", "days", "trend", "research", "watchlist", "outro"]
LLM_SECTIONS = ("intro", "days", "trend")

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]


# ── Research segments (fixed, rotate by ISO week) ─────────────────────────────
# Written once, deliberately qualitative: no metric that retraining could
# invalidate. Each is (title, bullets[3], spoken sentences).

RESEARCH_SEGMENTS: list[tuple[str, list[str], list[str]]] = [
    ("Why the model predicts regimes, not prices",
     ["Target: 24h trend regime", "Not a price target", "Direction, not magnitude"],
     ["A quick look inside the research this week.",
      "The model does not predict prices. It classifies the next twenty four "
      "hours into an upward, downward, or flat regime.",
      "Exact price targets are noise at this horizon. Regimes are the part of "
      "the problem a model can actually learn."]),
    ("High accuracy still is not a trading edge",
     ["Classification ≠ profit", "Fees and slippage bite", "That is why we publish"],
     ["A quick look inside the research this week.",
      "Our own published measurements show something most channels hide. A model "
      "can score well on classification metrics and still not be profitable to "
      "trade after fees.",
      "That finding is the reason this scoreboard exists in public."]),
    ("Why the model sometimes refuses to call a direction",
     ["Confidence gate", "Low conviction becomes HOLD", "Silence over guessing"],
     ["A quick look inside the research this week.",
      "When the ensemble's confidence falls below its threshold, a gate downgrades "
      "the call to hold.",
      "A forced guess would look decisive and grade like a coin flip. The gate "
      "would rather say nothing."]),
    ("Four architectures, one vote",
     ["Different model families", "Different failure modes", "Blended with learned weights"],
     ["A quick look inside the research this week.",
      "The ensemble mixes four model families on purpose. They read the market "
      "differently, so they fail differently.",
      "When their votes are blended, one model's blind spot is another model's "
      "signal. Disagreement between them is information, not a bug."]),
    ("How the models keep learning without forgetting",
     ["Continuous fine-tuning", "Replay buffers anchor history", "A gate blocks regressions"],
     ["A quick look inside the research this week.",
      "The models fine tune on fresh market data around the clock. A replay "
      "buffer keeps old training data in the mix, so adapting to this month "
      "cannot erase what two years of history taught them.",
      "Any update that scores worse than the current model is rejected outright."]),
    ("Why we grade on plain price moves",
     ["Anyone can verify a move", "No in-house metric", "Same rule every day"],
     ["A quick look inside the research this week.",
      "The scoreboard grades every call on the realised price move, with a flat "
      "band for small moves.",
      "We could have graded on the model's own training label, and scored better. "
      "But you cannot verify our label on your chart. You can verify a price move."]),
]


# ── Week data ─────────────────────────────────────────────────────────────────

def week_rows(now: datetime) -> list[dict]:
    """Resolved calls from the last 7 days, oldest first."""
    cutoff_ms = int((now - timedelta(days=7)).timestamp() * 1000)
    rows = [r for r in scoreboard._load()
            if r.get("resolved") and r.get("made_at_ms", 0) >= cutoff_ms]
    return sorted(rows, key=lambda r: r["made_at_ms"])


def build_facts(rows: list[dict], summary: dict, now: datetime) -> dict:
    iso = now.isocalendar()
    days = []
    for r in rows:
        made = datetime.fromtimestamp(r["made_at_ms"] / 1000, tz=timezone.utc)
        days.append({
            "weekday": WEEKDAYS[made.weekday()],
            "date": r["date"],
            "signal": r["signal"],
            "confidence_pct": r.get("confidence"),
            "gated": r.get("gated", False),
            "change_pct": r.get("change_pct"),
            "correct": r["correct"],
        })
    hits = sum(1 for r in rows if r["correct"])
    return {
        "week_number": iso.week,
        "date_range": f"{rows[0]['date']} to {rows[-1]['date']}" if rows else "",
        "days": days,
        "week_resolved": len(rows),
        "week_hits": hits,
        "week_misses": len(rows) - hits,
        "week_accuracy_pct": round(hits / len(rows) * 100, 1) if rows else None,
        "alltime_resolved": summary.get("resolved_calls"),
        "alltime_hits": summary.get("hits"),
        "alltime_misses": summary.get("misses"),
        "alltime_accuracy_pct": summary.get("accuracy_pct"),
        "flat_band_pct": summary.get("flat_band_pct"),
        "horizon_hours": summary.get("horizon_hours"),
    }


# ── Script ────────────────────────────────────────────────────────────────────

def validate(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("script is not a JSON object")
    for field in ("title", "description"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise ValueError(f"missing '{field}'")
    sections_in = raw.get("sections")
    if not isinstance(sections_in, dict):
        raise ValueError("missing 'sections'")
    sections = {}
    for sec in LLM_SECTIONS:
        val = sections_in.get(sec)
        if isinstance(val, str):
            val = [val]
        if not isinstance(val, list) or not val:
            raise ValueError(f"missing section '{sec}'")
        sections[sec] = [" ".join(str(s).split()) for s in val if str(s).strip()]

    words = sum(len(s.split()) for sec in sections.values() for s in sec)
    if not 80 <= words <= 260:
        raise ValueError(f"narration length out of range ({words} words)")

    tags = [str(t).lstrip("#").strip().lower()
            for t in (raw.get("tags") or []) if str(t).strip()]
    tags = list(dict.fromkeys(tags + config()["upload"]["base_tags"]))[:15]

    return {
        "title": " ".join(raw["title"].split())[:95],
        "description": raw["description"].strip(),
        "tags": tags,
        "sections": sections,
        "word_count": words,
    }


def write_script(facts: dict) -> dict:
    base = load_prompt("weekly_recap.md") + json.dumps(facts, indent=2, ensure_ascii=False)
    max_regen = config()["factcheck"]["max_regenerations"]
    feedback: list[str] = []

    for attempt in range(1, max_regen + 2):
        log.info(f"Writing weekly script (attempt {attempt}/{max_regen + 1})...")
        full = base
        if feedback:
            full += ("\n\n## CORRECTIONS REQUIRED\nYour previous attempt was rejected. "
                     "Fix every point:\n" + "\n".join(f"- {f}" for f in feedback))
        raw, provider = complete_json(full, label="weekly-script")
        try:
            script = validate(raw)
        except ValueError as e:
            feedback = [f"Invalid output shape: {e}"]
            continue

        problems = factcheck.verify(script, facts)
        if not problems:
            script["provider"] = provider
            log.info(f"  Weekly script by {provider}: \"{script['title']}\" "
                     f"({script['word_count']} words)")
            return script
        feedback = problems

    raise PipelineAbort(f"Weekly script failed verification {max_regen + 1} times. "
                        f"First problem: {feedback[0][:200]}")


# ── Scenes (1920x1080) ────────────────────────────────────────────────────────

def _wh() -> tuple[int, int]:
    w = config()["weekly"]
    return int(w["width"]), int(w["height"])


def _lheader(ax, w: int, label: str, week_no: int):
    t = R._theme()
    R._text(ax, 72, 84, "QUANTAURA", size=36, weight="bold", color=t["accent"])
    R._text(ax, w - 72, 84, f"WEEK {week_no}", size=34, color=t["muted"], ha="right")
    ax.plot([72, w - 72], [128, 128], color=t["grid"], lw=2)
    if label:
        R._text(ax, 72, 178, label.upper(), size=28, weight="bold", color=t["muted"])


def scene_title(facts: dict, path: Path) -> Path:
    w, h = _wh()
    t = R._theme()
    fig, ax = R._canvas(w, h)
    _lheader(ax, w, "the week in review", facts["week_number"])

    hits, res = facts["week_hits"], facts["week_resolved"]
    acc = facts.get("week_accuracy_pct")
    col = t["up"] if acc is not None and acc >= 50 else t["down"]

    R._text(ax, w / 2, 400, f"{hits} OF {res}", size=180, weight="bold",
            ha="center", color=col)
    R._text(ax, w / 2, 540, "CALLS CORRECT THIS WEEK", size=48, ha="center")
    R._text(ax, w / 2, 630, facts.get("date_range", ""), size=34,
            color=t["muted"], ha="center")

    # Outcome dots, oversized — the week at a glance.
    dot, gap = 64, 26
    days = facts["days"]
    strip = len(days) * dot + (len(days) - 1) * gap
    x = (w - strip) / 2
    for d in days:
        ax.add_patch(R.plt.Circle((x + dot / 2, 760), dot / 2,
                                  color=t["up"] if d["correct"] else t["down"], zorder=3))
        R._text(ax, x + dot / 2, 760, "✓" if d["correct"] else "✗",
                size=30, weight="bold", color=t["bg"], ha="center")
        R._text(ax, x + dot / 2, 840, d["weekday"][:3].upper(), size=24,
                color=t["muted"], ha="center")
        x += dot + gap

    R._text(ax, w / 2, h - 90, "graded on the realised 24-hour move · flat band ±"
            f"{facts.get('flat_band_pct', 1)}%", size=28, color=t["muted"], ha="center")
    return R._save(fig, path)


def scene_days(facts: dict, path: Path) -> Path:
    w, h = _wh()
    t = R._theme()
    fig, ax = R._canvas(w, h)
    _lheader(ax, w, "every call, graded", facts["week_number"])

    days = facts["days"]
    # Header row sits below the scene label, not on top of it; rows then fill
    # the remaining height.
    top = 300
    row_h = min(106, (h - top - 50) // max(len(days), 1))
    cols = {"day": 110, "call": 470, "conf": 800, "move": 1150, "verdict": w - 180}

    R._text(ax, cols["day"], top - 46, "DAY", size=26, color=t["muted"])
    R._text(ax, cols["call"], top - 46, "CALL", size=26, color=t["muted"])
    R._text(ax, cols["conf"], top - 46, "CONFIDENCE", size=26, color=t["muted"])
    R._text(ax, cols["move"], top - 46, "24H MOVE", size=26, color=t["muted"])
    R._text(ax, cols["verdict"], top - 46, "RESULT", size=26, color=t["muted"], ha="right")

    y = top
    for d in days:
        R._panel(ax, 84, y, w - 168, row_h - 14, color=t["panel"])
        cy = y + (row_h - 14) / 2
        R._text(ax, cols["day"], cy, d["weekday"], size=34, weight="bold")
        sig = d["signal"]
        # The gate marker is a small tag, not part of the signal word — at
        # font 34 "SELL (gated)" ran into the confidence column.
        R._text(ax, cols["call"], cy - (12 if d.get("gated") else 0), sig,
                size=34, weight="bold", color=R._signal_color(sig))
        if d.get("gated"):
            R._text(ax, cols["call"], cy + 26, "confidence gate", size=20,
                    color=t["flat"])
        conf = d.get("confidence_pct")
        R._text(ax, cols["conf"], cy, f"{conf:.1f}%" if conf is not None else "—", size=32)
        chg = d.get("change_pct")
        if chg is not None:
            R._text(ax, cols["move"], cy, f"{chg:+.2f}%", size=32,
                    color=t["up"] if chg >= 0 else t["down"])
        hit = d["correct"]
        R._text(ax, cols["verdict"], cy, "HIT" if hit else "MISS", size=36,
                weight="bold", color=t["up"] if hit else t["down"], ha="right")
        y += row_h
    return R._save(fig, path)


def scene_trend(facts: dict, candles: list[list] | None, path: Path) -> Path:
    w, h = _wh()
    t = R._theme()
    fig, ax = R._canvas(w, h)
    _lheader(ax, w, "the week on the chart", facts["week_number"])

    x0, x1, y0, y1 = 120, w - 120, 280, h - 260
    if candles and len(candles) >= 8:
        closes = [c[4] for c in candles]
        times = [c[0] for c in candles]
        lo, hi = min(closes), max(closes)
        span = (hi - lo) or 1.0

        def px(ms: float) -> float:
            return x0 + (x1 - x0) * (ms - times[0]) / max(times[-1] - times[0], 1)

        def py(v: float) -> float:
            return y1 - (y1 - y0) * ((v - lo) / span)

        for frac in (0, 0.5, 1.0):
            gy = y0 + (y1 - y0) * frac
            ax.plot([x0, x1], [gy, gy], color=t["grid"], lw=1.5, zorder=2)

        xs = [px(ms) for ms in times]
        ys = [py(v) for v in closes]
        up = closes[-1] >= closes[0]
        line = t["up"] if up else t["down"]
        ax.plot(xs, ys, color=line, lw=4, zorder=4, solid_capstyle="round")
        ax.fill_between(xs, ys, [y1] * len(xs), color=line, alpha=0.10, zorder=3)
        R._text(ax, x1, y0 - 30, f"${hi:,.0f}", size=26, color=t["muted"], ha="right")
        R._text(ax, x1, y1 + 34, f"${lo:,.0f}", size=26, color=t["muted"], ha="right")

        # Each graded call, planted on the chart at the moment it was made.
        for d, row in zip(facts["days"], facts["_rows"]):
            ms = row["made_at_ms"]
            if not times[0] <= ms <= times[-1]:
                continue
            cx = px(ms)
            price_then = row.get("price")
            cy = py(price_then) if price_then else y0 + 40
            col = R._signal_color(d["signal"])
            ax.add_patch(R.plt.Circle((cx, cy), 17, color=col, zorder=6))
            ax.add_patch(R.plt.Circle((cx, cy), 24, fill=False, zorder=6, linewidth=4,
                                      edgecolor=t["up"] if d["correct"] else t["down"]))
            R._text(ax, cx, cy - 56, d["signal"][0], size=28, weight="bold",
                    color=col, ha="center")
    else:
        R._text(ax, w / 2, (y0 + y1) / 2, "chart unavailable this week",
                size=36, color=t["muted"], ha="center")

    R._text(ax, x0, y1 + 34, "the week's price · dot = the call · ring = the outcome",
            size=26, color=t["muted"])
    return R._save(fig, path)


def scene_research(segment: tuple[str, list[str], list[str]], week_no: int,
                   path: Path) -> Path:
    w, h = _wh()
    t = R._theme()
    fig, ax = R._canvas(w, h)
    _lheader(ax, w, "inside the research", week_no)

    title, bullets, _ = segment
    R._fit_text(fig, ax, 360, title, 72, max_w=w - 240, x=w / 2, weight="bold")
    y = 520
    for b in bullets:
        R._panel(ax, w / 2 - 560, y, 1120, 118, color=t["panel"])
        ax.plot([w / 2 - 520, w / 2 - 520], [y + 32, y + 86], color=t["accent"],
                lw=8, solid_capstyle="round", zorder=3)
        R._text(ax, w / 2 - 480, y + 59, b, size=38)
        y += 146
    R._text(ax, w / 2, h - 110, "full methodology at quantaura.tech",
            size=30, color=t["muted"], ha="center")
    return R._save(fig, path)


def scene_watchlist(week_no: int, path: Path) -> Path:
    w, h = _wh()
    t = R._theme()
    fig, ax = R._canvas(w, h)
    _lheader(ax, w, "beyond bitcoin", week_no)

    R._text(ax, w / 2, 320, "The ensemble also covers", size=44, ha="center",
            color=t["muted"])
    symbols = ["ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX"]
    bw, gap = 220, 24
    per_row = 4
    for i, s in enumerate(symbols):
        row, col_i = divmod(i, per_row)
        count = min(per_row, len(symbols) - row * per_row)
        row_w = count * bw + (count - 1) * gap
        x = (w - row_w) / 2 + col_i * (bw + gap)
        y = 420 + row * 180
        R._panel(ax, x, y, bw, 140, color=t["panel"])
        R._text(ax, x + bw / 2, y + 70, s, size=48, weight="bold", ha="center")

    R._text(ax, w / 2, 850, "Daily calls for every symbol live at",
            size=38, ha="center")
    R._text(ax, w / 2, 930, "quantaura.tech", size=64, weight="bold",
            color=t["accent"], ha="center")
    return R._save(fig, path)


def scene_outro_wide(week_no: int, path: Path) -> Path:
    w, h = _wh()
    t = R._theme()
    fig, ax = R._canvas(w, h)
    _lheader(ax, w, "", week_no)
    R._text(ax, w / 2, 380, "NOT FINANCIAL ADVICE", size=84, weight="bold", ha="center")
    for i, line in enumerate(R._wrap(
            "An automated research project. The model is often wrong — "
            "that is why the scoreboard is public.", 52)[:3]):
        R._text(ax, w / 2, 540 + i * 60, line, size=38, color=t["muted"], ha="center")
    R._text(ax, w / 2, 800, "A new call, every day, in Shorts", size=44,
            weight="bold", color=t["accent"], ha="center")
    return R._save(fig, path)


# ── Fixed narration ───────────────────────────────────────────────────────────

def fixed_sentences(segment: tuple[str, list[str], list[str]]) -> list[tuple[str, str]]:
    pairs = [("research", s) for s in segment[2]]
    pairs += [("watchlist",
               "The ensemble does not only watch Bitcoin. It covers Ethereum, "
               "Solana, and five other majors every day."),
              ("watchlist",
               "Those daily calls live on quantaura dot tech, alongside the full "
               "research.")]
    pairs.append(("outro", config()["disclaimer"]["narration"]))
    pairs.append(("outro", "The daily calls are on this channel every day as Shorts. "
                           "The scoreboard never stops."))
    return pairs


# ── Assembly ──────────────────────────────────────────────────────────────────

def assemble(facts: dict, script: dict, candles: list[list] | None,
             narration: V.Narration, segment) -> Path:
    w, h = _wh()
    wk = facts["week_number"]
    cfg = config()["video"]
    xf = float(cfg["transition_seconds"])

    spans = narration.section_spans(SECTIONS)
    scenes = [
        ("intro", scene_title(facts, BUILD_DIR / "wk_title.png")),
        ("days", scene_days(facts, BUILD_DIR / "wk_days.png")),
        ("trend", scene_trend(facts, candles, BUILD_DIR / "wk_trend.png")),
        ("research", scene_research(segment, wk, BUILD_DIR / "wk_research.png")),
        ("watchlist", scene_watchlist(wk, BUILD_DIR / "wk_watchlist.png")),
        ("outro", scene_outro_wide(wk, BUILD_DIR / "wk_outro.png")),
    ]

    clips, durations = [], []
    for i, (sec, png) in enumerate(scenes):
        d = max(spans.get(sec, 0.0), 2.0)
        clips.append(R._still_clip(png, d + xf, BUILD_DIR / f"wk_clip_{i}.mp4",
                                   drift=(sec != "outro"), w=w, h=h))
        durations.append(d)

    log.info("Assembling weekly body...")
    body = R._crossfade(clips, durations, BUILD_DIR / "wk_body_silent.mp4")

    wcfg = config()["weekly"]
    ass = R.build_captions(narration, BUILD_DIR / "wk_captions.ass", w=w, h=h,
                           fontsize=int(wcfg["caption_fontsize"]),
                           margin_v=int(wcfg["caption_margin_v"]), wrap_chars=60)
    final = BUILD_DIR / "weekly.mp4"
    escaped = ass.as_posix().replace(":", r"\:")
    R._run(["-i", str(body), "-i", str(narration.wav_path),
            "-vf", f"subtitles='{escaped}'",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-shortest",
            str(final)], "weekly mux")
    return final


# ── Dry-run fixtures ──────────────────────────────────────────────────────────

def _dry_rows(now: datetime) -> list[dict]:
    out = []
    # Row 3 is the gated day: a gated call always DISPLAYS as HOLD — the gate
    # downgrades a directional call, so gated=True with signal SELL cannot
    # occur in real data and the fixture must not invent it.
    outcomes = [("HOLD", 0.4, True), ("HOLD", -0.2, True), ("BUY", 2.1, True),
                ("HOLD", 1.3, False), ("BUY", -1.8, False), ("HOLD", 0.9, True),
                ("BUY", 1.6, True)]
    for i, (sig, chg, ok) in enumerate(outcomes):
        made = now - timedelta(days=7 - i)
        out.append({"date": made.strftime("%Y-%m-%d"), "made_at_ms":
                    int(made.timestamp() * 1000), "symbol": "BTCUSDT",
                    "signal": sig, "confidence": 52.0 + i * 4, "gated": i == 3,
                    "price": 63000 + i * 300, "resolved": True,
                    "change_pct": chg, "correct": ok})
    return out


def _dry_script(facts: dict) -> dict:
    return {
        "title": f"BTC Model Week {facts['week_number']}: "
                 f"{facts['week_hits']} of {facts['week_resolved']} Correct — dry run",
        "description": "Dry-run build of the weekly recap. Not uploaded.",
        "tags": ["bitcoin", "machine learning"],
        "sections": {
            "intro": ["Five of seven calls landed this week, the model's best "
                      "run so far."],
            "days": ["The week opened with two holds, both correct.",
                     "Thursday was the miss that stung. A sell call, and the "
                     "market barely moved."],
            "trend": ["The all-time record now stands at five of seven graded "
                      "calls."],
        },
        "word_count": 52,
        "provider": "dry",
    }


# ── Blog ──────────────────────────────────────────────────────────────────────

def _publish_blog(facts: dict, script: dict, summary: dict, segment,
                  video_url: str | None, dry_run: bool) -> None:
    """The same reviewed material, rendered as a written post (see blogpost.py).

    Non-fatal on purpose: the video is the primary product, and a blog write
    error after a successful upload must not turn a published run into a
    crashed one.
    """
    try:
        import blogpost
        blogpost.publish(facts, script, summary, segment, video_url,
                         dry_run=dry_run)
    except Exception as e:                                        # noqa: BLE001
        log.warning(f"  Blog post generation failed (non-fatal): "
                    f"{type(e).__name__}: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    stage = "startup"
    now = datetime.now(timezone.utc)
    try:
        publishing = not (args.dry_run or args.no_upload)
        if publishing and not args.force and published_today("weekly"):
            log.info("A weekly recap was already published today — nothing to do.")
            return 0

        stage = "collect"
        if args.dry_run:
            log.info("DRY RUN — synthetic week, no live calls")
            rows = _dry_rows(now)
            summary = {"resolved_calls": 7, "hits": 5, "misses": 2,
                       "accuracy_pct": 71.4, "flat_band_pct": 1.0, "horizon_hours": 24}
            candles = None
        else:
            scoreboard.resolve_due()
            summary = scoreboard.summary()
            rows = week_rows(now)
            # 8 days, not 7: the week's oldest call (last Sunday 13:00) sits
            # just outside a trailing 7-day window fetched at 15:00, and its
            # marker would silently drop off the chart every single week.
            candles = prices.candles(config()["channel"]["symbol"], "1h", 8 * 24)

        min_needed = int(config()["weekly"]["min_resolved_calls"])
        if len(rows) < min_needed:
            raise PipelineAbort(
                f"Only {len(rows)} graded calls in the last 7 days (need "
                f"{min_needed}). A recap of that would be filler — skipping "
                f"this week.")

        facts = build_facts(rows, summary, now)
        write_json(BUILD_DIR / "weekly_facts.json", facts)

        stage = "script"
        script = _dry_script(facts) if args.dry_run else write_script(facts)
        write_json(BUILD_DIR / "weekly_script.json", script)

        stage = "voice"
        segment = RESEARCH_SEGMENTS[facts["week_number"] % len(RESEARCH_SEGMENTS)]
        sentences = [(sec, s) for sec in LLM_SECTIONS
                     for s in script["sections"][sec]]
        sentences += fixed_sentences(segment)
        narration = V.synthesize(sentences, out_name="weekly_narration.wav",
                                 silent=args.no_voice or args.dry_run)

        stage = "render"
        facts["_rows"] = rows           # scene_trend needs made_at_ms + price
        video = assemble(facts, script, candles, narration, segment)
        duration = R.duration_of(video)
        log.info(f"  Weekly video: {duration:.1f}s" if duration else "  Weekly video ready")

        stage = "thumbnail"
        import thumbnail
        thumb = thumbnail.build_weekly(facts)

        stage = "upload"
        if args.no_upload or args.dry_run:
            reason = "--dry-run" if args.dry_run else "--no-upload"
            _publish_blog(facts, script, summary, segment, None, args.dry_run)
            record_run("rendered", stage, f"Weekly built, not uploaded ({reason}).",
                       {"video": str(video), "duration_s": round(duration or 0, 1)},
                       kind="weekly")
            log.info(f"Skipping upload ({reason}). Video at {video}")
            return 0

        import upload as U
        desc = (script["description"]
                + "\n\nEVERY CALL THIS WEEK\n"
                + "\n".join(f"{d['weekday']}: {d['signal']} → {d['change_pct']:+.2f}% "
                            f"({'hit' if d['correct'] else 'miss'})"
                            for d in facts["days"])
                + f"\n\nAll-time: {facts['alltime_hits']} of "
                  f"{facts['alltime_resolved']} graded calls correct."
                + "\n\nTHE REST OF THE MODEL\n"
                + "\n".join(U._link_lines(config()))
                + "\n\nThis video was generated automatically. The narration "
                  "voice is synthetic.\n"
                + config()["disclaimer"]["description"].strip())
        try:
            _vid, url = U.publish(video, thumb, title=script["title"],
                                  description=desc, tags=script["tags"])
        except PipelineAbort as e:
            log.error(f"Weekly upload failed: {e}")
            # The facts are graded and reviewed regardless of the upload — the
            # written post still publishes, just without a video link.
            _publish_blog(facts, script, summary, segment, None, False)
            record_run("rendered", "upload", str(e), {"video": str(video)},
                       kind="weekly")
            notify.rendered_only({}, script, str(video), str(e))
            return 0

        _publish_blog(facts, script, summary, segment, url, False)
        record_run("published", "upload", script["title"],
                   {"url": url, "visibility": config()["upload"]["visibility"],
                    "duration_s": round(duration or 0, 1)}, kind="weekly")
        notify.weekly_published(script["title"], url, facts,
                                config()["upload"]["visibility"])
        log.info(f"Done: {url}")
        return 0

    except PipelineAbort as e:
        log.error(f"Weekly aborted at '{stage}': {e}")
        record_run("skipped", stage, str(e), kind="weekly")
        notify.skipped(str(e), f"weekly/{stage}")
        return 0
    except Exception as e:                                        # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        log.error(f"Weekly crashed at '{stage}': {detail}")
        traceback.print_exc()
        record_run("crashed", stage, detail, kind="weekly")
        notify.crashed(f"{detail}\n\n{traceback.format_exc()[-600:]}",
                       f"weekly/{stage}")
        return 1


def main() -> int:
    p = argparse.ArgumentParser(description="QuantAura weekly recap")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--no-voice", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="publish even if a recap already went out today")
    args = p.parse_args()
    log.info("=" * 60)
    log.info(f"QuantAura weekly recap · {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    log.info("=" * 60)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
