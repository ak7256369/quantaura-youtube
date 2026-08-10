"""The Sunday long-form recap: every graded call this week, reviewed on camera.

    python weekly.py                 # build + upload per config.yaml
    python weekly.py --no-upload     # build only
    python weekly.py --no-voice      # silent placeholder audio (layout work)
    python weekly.py --dry-run       # synthetic week, no API calls, no upload

Format: 1920x1080, ~4 minutes. Ten scenes:
    title     → the week's record, huge
    days      → the call-by-call table
    spotlight → the one call that defined the week, reviewed on its own
    trend     → week price chart with each call marked, hit or miss
    portfolio → the paper $10k traded on those calls, next to buy-and-hold
    record    → where the all-time scoreboard stands after this week
    method    → how a call is made and graded (fixed)
    research  → one rotating segment on how the model actually works (fixed)
    watchlist → the other symbols the ensemble covers (CTA, no calls shown)
    outro     → disclaimer

Length is narration-bound and nothing else: every scene lasts exactly as long
as the sentences spoken over it (see `assemble`). Reaching four minutes is
therefore a word budget, not a rendering setting — see `validate` and
prompts/weekly_recap.md, which have to move together.

Six narrative sections are LLM-written from the graded rows and pass the same
fact-check gates as the daily video. `method`, `research`, `watchlist` and
`outro` are fixed prose written once, because their content must stay true
regardless of what any model returns.

The dense scenes build themselves in stage by stage rather than appearing whole
(`_staged_clip`). At four minutes a card can hold the screen for half a minute,
and a static half-minute is where a viewer leaves.

The watchlist deliberately shows WHICH symbols the ensemble covers and never
what it says about them today — those calls are the site's premium feature,
and giving them away daily would unmake the channel's own funnel.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import drive as drive_mod                                          # noqa: E402
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

SECTIONS = ["intro", "days", "spotlight", "trend", "portfolio", "record",
            "method", "research", "watchlist", "outro"]
LLM_SECTIONS = ("intro", "days", "spotlight", "trend", "portfolio", "record")
# Dropped from both the script and the scene list when the facts cannot support
# them. The paper portfolio needs priced rows; a week that somehow has none
# should lose the segment, not stall the recap on a section the model cannot
# write honestly.
OPTIONAL_LLM_SECTIONS = ("portfolio",)
FIXED_SECTIONS = ("method", "research", "watchlist", "outro")

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]

FRAMES_DIR = BUILD_DIR / "wk_stages"


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
      "the problem a model can actually learn.",
      "Ask a model for a number it cannot know and it will hand you one anyway. "
      "We would rather ask it a question it can answer."]),
    ("High accuracy still is not a trading edge",
     ["Classification ≠ profit", "Fees and slippage bite", "That is why we publish"],
     ["A quick look inside the research this week.",
      "Our own published measurements show something most channels hide. A model "
      "can score well on classification metrics and still not be profitable to "
      "trade after fees.",
      "That finding is the reason this scoreboard exists in public.",
      "So the paper portfolio sits next to the accuracy in these recaps, and the "
      "two numbers are allowed to disagree in front of you."]),
    ("Why the model sometimes refuses to call a direction",
     ["Confidence gate", "Low conviction becomes HOLD", "Silence over guessing"],
     ["A quick look inside the research this week.",
      "When the ensemble's confidence falls below its threshold, a gate downgrades "
      "the call to hold.",
      "A forced guess would look decisive and grade like a coin flip. The gate "
      "would rather say nothing.",
      "That makes hold the most common call here, and it is meant to be. A model "
      "with a strong opinion every single day is telling you about itself rather "
      "than about the market."]),
    ("Four architectures, one vote",
     ["Different model families", "Different failure modes", "Blended with learned weights"],
     ["A quick look inside the research this week.",
      "The ensemble mixes four model families on purpose. They read the market "
      "differently, so they fail differently.",
      "When their votes are blended, one model's blind spot is another model's "
      "signal. Disagreement between them is information, not a bug.",
      "None of the four is the best model. Picking a favourite would mean betting "
      "everything on one way of being wrong."]),
    ("How the models keep learning without forgetting",
     ["Continuous fine-tuning", "Replay buffers anchor history", "A gate blocks regressions"],
     ["A quick look inside the research this week.",
      "The models fine tune on fresh market data around the clock. A replay "
      "buffer keeps old training data in the mix, so adapting to this month "
      "cannot erase what two years of history taught them.",
      "Any update that scores worse than the current model is rejected outright.",
      "Catastrophic forgetting is the failure that guards against. A model tuned "
      "only on the last month becomes an expert on the last month and a novice "
      "everywhere else."]),
    ("Why we grade on plain price moves",
     ["Anyone can verify a move", "No in-house metric", "Same rule every day"],
     ["A quick look inside the research this week.",
      "The scoreboard grades every call on the realised price move, with a flat "
      "band for small moves.",
      "We could have graded on the model's own training label, and scored better. "
      "But you cannot verify our label on your chart. You can verify a price move.",
      "The harsher rule is the one worth publishing. The kinder one would only "
      "measure how generous we were with ourselves."]),
]


# ── The method segment (fixed) ────────────────────────────────────────────────
# Evergreen: a viewer who arrives at any week needs to know what the number on
# screen actually means before the rest of the video is worth anything.

METHOD_STEPS = [
    ("ONE CALL A DAY", "BUY, SELL or HOLD for the next 24 hours"),
    ("WRITTEN DOWN FIRST", "logged with the price, before the outcome exists"),
    ("GRADED ON THE MOVE", "±1% flat band decides direction or flat"),
    ("NEVER EDITED", "the rule does not change to suit the result"),
]


# One sentence per card, in order: the scene deals a card as its sentence is
# spoken (see `_sentence_cuts`), so these two lists must stay the same length.
METHOD_SENTENCES = [
    "Here is exactly how a call is graded, so none of this rests on trusting us.",
    "Once a day the ensemble publishes a single call for the next twenty four "
    "hours, written down with the price before the outcome can be known.",
    "Twenty four hours later the realised move settles it. A rise or fall of more "
    "than one percent is a direction, and anything smaller counts as flat.",
    "The rule never bends to suit the result, and no call is edited once it has "
    "been graded.",
]


def _assert_method_matches_config() -> None:
    """The method narration states the horizon and flat band as spoken words.

    Change either config value and that fixed prose silently becomes a lie —
    the same drift that make_intro_videos.py guards against, and the same fix:
    fail the run rather than narrate a rule the scoreboard is no longer using.
    """
    sc = config()["scoring"]
    if int(sc["horizon_hours"]) != 24 or float(sc["flat_band_pct"]) != 1.0:
        raise PipelineAbort(
            f"METHOD_SENTENCES and METHOD_STEPS say 24 hours and ±1%, but scoring "
            f"is now {sc['horizon_hours']}h / ±{sc['flat_band_pct']}%. Rewrite the "
            f"method segment before publishing.")


# ── Week data ─────────────────────────────────────────────────────────────────

def week_rows(now: datetime) -> list[dict]:
    """Resolved calls from the last 7 days, oldest first."""
    cutoff_ms = int((now - timedelta(days=7)).timestamp() * 1000)
    rows = [r for r in scoreboard._load()
            if r.get("resolved") and r.get("made_at_ms", 0) >= cutoff_ms]
    return sorted(rows, key=lambda r: r["made_at_ms"])


def _notable(days: list[dict]) -> dict | None:
    """The single call the spotlight scene reviews — chosen by rule, not by the
    model.

    Misses outrank hits, and within the pool the largest realised move wins: the
    week's most expensive wrong call is the one a viewer following along would
    actually have felt. Letting an LLM choose which day to dwell on would hand
    it exactly the editorial discretion the rest of this pipeline denies it, and
    the first thing a model optimises for is a flattering week.
    """
    if not days:
        return None
    misses = [d for d in days if not d["correct"]]
    pick = max(misses or days, key=lambda d: abs(d.get("change_pct") or 0.0))
    return {**pick, "kind": "miss" if not pick["correct"] else "hit"}


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
    port = summary.get("portfolio") or None
    streak = summary.get("streak") or {}
    return {
        "week_number": iso.week,
        # ISO year, not calendar year: week 1 can start in December, and the
        # Drive filename is keyed on this pair.
        "iso_year": iso.year,
        "date_range": f"{rows[0]['date']} to {rows[-1]['date']}" if rows else "",
        "days": days,
        "week_resolved": len(rows),
        "week_hits": hits,
        "week_misses": len(rows) - hits,
        "week_accuracy_pct": round(hits / len(rows) * 100, 1) if rows else None,
        "notable": _notable(days),
        "alltime_resolved": summary.get("resolved_calls"),
        "alltime_hits": summary.get("hits"),
        "alltime_misses": summary.get("misses"),
        "alltime_accuracy_pct": summary.get("accuracy_pct"),
        "alltime_accuracy_30d_pct": summary.get("accuracy_30d_pct"),
        "alltime_resolved_30d": summary.get("resolved_30d"),
        "streak_kind": streak.get("kind"),
        "streak_length": streak.get("length"),
        "by_signal": summary.get("by_signal") or {},
        # Headline figures only — the day-by-day `curve` is deliberately left
        # out. factcheck._numbers_in whitelists every number reachable in the
        # facts, so handing it a few hundred daily valuations would quietly
        # licence the model to say almost any four-or-five-digit figure. The
        # curve reaches the renderer through facts["_portfolio_curve"], which is
        # attached after the script has been checked.
        "portfolio": ({
            "start_usd": port["start_usd"],
            "days": port["days"],
            "value_usd": port["value_usd"],
            "return_pct": port["return_pct"],
            "hold_value_usd": port["hold_value_usd"],
            "hold_return_pct": port["hold_return_pct"],
            "vs_hold_pct": port["vs_hold_pct"],
            "fees_paid_usd": port["fees_paid_usd"],
            "fee_pct_per_side": port["fee_pct_per_side"],
            "trades": port["trades"],
            "position": port["position"],
        } if port else None),
        "flat_band_pct": summary.get("flat_band_pct"),
        "horizon_hours": summary.get("horizon_hours"),
    }


def active_sections(facts: dict) -> list[str]:
    """Scene order for this week, minus any optional section the facts cannot
    support. Narration, scenes and `section_spans` all read this one list, so
    they cannot drift out of agreement."""
    return [s for s in SECTIONS
            if s not in OPTIONAL_LLM_SECTIONS or facts.get(s) is not None]


# ── Script ────────────────────────────────────────────────────────────────────

def word_budget(required: tuple[str, ...] | list[str]) -> tuple[int, int]:
    """Accepted narration length, in words, for a given set of sections.

    Scaled per section rather than fixed, because the section list is not
    fixed: dropping the portfolio segment should relax the floor by one
    segment's worth of prose, not fail a script that is correctly shorter.

    The absolute numbers are what set the video's runtime, measured rather than
    guessed: the 2026-08-09 recap read 267 words in 110.0s, so Kokoro at
    speed 1.02 delivers ~152 words per minute including sentence gaps. About
    215 words of fixed prose (method, research, watchlist, outro) are appended
    in code afterwards, so a mid-range 390 LLM words lands the finished recap
    at roughly four minutes. Moving this range moves the runtime, and nothing
    else does — see the module docstring.
    """
    return 58 * len(required), 72 * len(required)


def validate(raw: dict, required: tuple[str, ...] | list[str] = LLM_SECTIONS) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("script is not a JSON object")
    for field in ("title", "description"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise ValueError(f"missing '{field}'")
    sections_in = raw.get("sections")
    if not isinstance(sections_in, dict):
        raise ValueError("missing 'sections'")
    sections = {}
    for sec in required:
        val = sections_in.get(sec)
        if isinstance(val, str):
            val = [val]
        if not isinstance(val, list) or not val:
            raise ValueError(f"missing section '{sec}'")
        sections[sec] = [" ".join(str(s).split()) for s in val if str(s).strip()]

    words = sum(len(s.split()) for sec in sections.values() for s in sec)
    lo, hi = word_budget(required)
    if not lo <= words <= hi:
        raise ValueError(f"narration length out of range ({words} words, "
                         f"need {lo}-{hi} across {len(required)} sections)")

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


def write_script(facts: dict, required: tuple[str, ...] | list[str]) -> dict:
    lo, hi = word_budget(required)
    base = (load_prompt("weekly_recap.md")
            # The prompt file cannot state the budget itself: it depends on
            # which optional sections survived into `required` this week.
            .replace("{SECTION_LIST}", ", ".join(f"`{s}`" for s in required))
            .replace("{WORD_LO}", str(lo)).replace("{WORD_HI}", str(hi))
            + json.dumps(facts, indent=2, ensure_ascii=False))
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
            script = validate(raw, required)
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


def scene_days(facts: dict, path: Path, rows_shown: int | None = None) -> Path:
    """The week's table. `rows_shown` draws only the first N rows, so the scene
    can deal itself out one day at a time (see `_days_stages`)."""
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
    # Row geometry is computed from the full week above, so a partially dealt
    # table never reflows as the remaining rows arrive.
    for d in days[:rows_shown if rows_shown is not None else len(days)]:
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


def scene_spotlight(facts: dict, path: Path) -> Path:
    """One call, blown up. The week's table shows everything and therefore
    emphasises nothing; this card is where a single day is allowed to land."""
    w, h = _wh()
    t = R._theme()
    fig, ax = R._canvas(w, h)
    n = facts.get("notable") or {}
    miss = n.get("kind") == "miss"
    _lheader(ax, w, "the call that defined the week", facts["week_number"])

    R._text(ax, w / 2, 268, "THE WEEK'S BIGGEST MISS" if miss else "THE WEEK'S BIGGEST MOVE",
            size=40, weight="bold", color=t["down"] if miss else t["up"], ha="center")
    R._text(ax, w / 2, 350, f"{n.get('weekday', '')} · {n.get('date', '')}",
            size=36, color=t["muted"], ha="center")

    sig = n.get("signal", "—")
    R._text(ax, w / 2, 520, sig, size=180, weight="bold",
            color=R._signal_color(sig), ha="center")
    if n.get("gated"):
        R._text(ax, w / 2, 630, "downgraded by the confidence gate", size=32,
                color=t["flat"], ha="center")

    # Three tiles: what the model said, what the market did, how it graded.
    conf, chg = n.get("confidence_pct"), n.get("change_pct")
    tiles = [("CONFIDENCE", f"{conf:.1f}%" if conf is not None else "—", t["text"]),
             ("24H MOVE", f"{chg:+.2f}%" if chg is not None else "—",
              t["up"] if (chg or 0) >= 0 else t["down"]),
             ("RESULT", "MISS" if miss else "HIT", t["down"] if miss else t["up"])]
    bw, gap = 460, 40
    x = (w - (len(tiles) * bw + (len(tiles) - 1) * gap)) / 2
    for label, value, colour in tiles:
        R._panel(ax, x, 720, bw, 190, color=t["panel"])
        R._text(ax, x + bw / 2, 780, label, size=28, color=t["muted"], ha="center")
        R._text(ax, x + bw / 2, 860, value, size=68, weight="bold", color=colour,
                ha="center")
        x += bw + gap

    R._text(ax, w / 2, h - 80, "logged before the outcome was knowable",
            size=28, color=t["muted"], ha="center")
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


def scene_portfolio(facts: dict, curve: list[dict], path: Path,
                    reveal: float = 1.0) -> Path:
    """The paper $10k against buy-and-hold. `reveal` draws a prefix of both
    lines so the two can be watched separating rather than presented apart."""
    w, h = _wh()
    t = R._theme()
    fig, ax = R._canvas(w, h)
    p = facts.get("portfolio") or {}
    _lheader(ax, w, "the paper portfolio", facts["week_number"])

    # Bottom stops at h-260, not h-200: the two caption lines and the verdict
    # below the chart need the room.
    x0, x1, y0, y1 = 120, w - 640, 300, h - 260
    pts = curve[:max(2, int(len(curve) * reveal))] if len(curve) >= 2 else []
    if pts:
        vals = [c["value"] for c in curve] + [c["hold"] for c in curve]
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0

        def px(i: int) -> float:
            return x0 + (x1 - x0) * (i / max(len(curve) - 1, 1))

        def py(v: float) -> float:
            return y1 - (y1 - y0) * ((v - lo) / span)

        for frac in (0, 0.5, 1.0):
            gy = y0 + (y1 - y0) * frac
            ax.plot([x0, x1], [gy, gy], color=t["grid"], lw=1.5, zorder=2)

        xs = [px(i) for i in range(len(pts))]
        ax.plot(xs, [py(c["hold"]) for c in pts], color=t["muted"], lw=4,
                zorder=3, solid_capstyle="round", linestyle=(0, (6, 6)))
        ax.plot(xs, [py(c["value"]) for c in pts], color=t["accent"], lw=5,
                zorder=4, solid_capstyle="round")
        # 620px apart, not 470: "FOLLOWING THE CALLS" measures ~470px in bold at
        # this size, so the two legend labels were rendering flush against each
        # other with no gap at all.
        R._text(ax, x0, y0 - 40, "FOLLOWING THE CALLS", size=26,
                weight="bold", color=t["accent"])
        R._text(ax, x0 + 620, y0 - 40, "JUST HOLDING", size=26, color=t["muted"])
        R._text(ax, x1, y0 - 40, f"${hi:,.0f}", size=24, color=t["muted"], ha="right")
        R._text(ax, x1, y1 + 36, f"${lo:,.0f}", size=24, color=t["muted"], ha="right")
    else:
        R._text(ax, (x0 + x1) / 2, (y0 + y1) / 2, "curve unavailable",
                size=34, color=t["muted"], ha="center")

    ret, hold = p.get("return_pct"), p.get("hold_return_pct")
    behind = ret is not None and hold is not None and ret < hold
    tiles = [("VALUE NOW", f"${p.get('value_usd', 0):,.0f}", t["text"]),
             ("FOLLOWING THE CALLS", f"{ret:+.2f}%" if ret is not None else "—",
              t["up"] if (ret or 0) >= 0 else t["down"]),
             ("JUST HOLDING", f"{hold:+.2f}%" if hold is not None else "—",
              t["up"] if (hold or 0) >= 0 else t["down"]),
             ("FEES PAID", f"${p.get('fees_paid_usd', 0):,.2f}", t["muted"])]
    tx, ty, tw = w - 580, 300, 460
    for label, value, colour in tiles:
        R._panel(ax, tx, ty, tw, 150, color=t["panel"])
        R._text(ax, tx + 34, ty + 48, label, size=25, color=t["muted"])
        R._text(ax, tx + 34, ty + 108, value, size=54, weight="bold", color=colour)
        ty += 170

    # Stated plainly whichever way it went. The tradeability research says this
    # line probably reads "behind" more often than not, and printing it anyway
    # is the entire reason the paper portfolio is on the channel.
    #
    # Centred on the chart, not the frame: the tile column owns the right third,
    # and a full-width caption runs underneath it.
    trades = p.get("trades", 0)
    caption = (f"${p.get('start_usd', 10000):,.0f} traded mechanically on the "
               f"committed calls · {trades} trade{'' if trades == 1 else 's'} · "
               f"fees included")
    for i, line in enumerate(R._wrap(caption, 46)[:2]):
        R._fit_text(fig, ax, h - 165 + i * 40, line, 26, max_w=x1 - x0,
                    x=(x0 + x1) / 2, min_size=18, color=t["muted"])
    R._text(ax, (x0 + x1) / 2, h - 60,
            "behind buy-and-hold" if behind else "ahead of buy-and-hold",
            size=30, weight="bold", color=t["down"] if behind else t["up"], ha="center")
    return R._save(fig, path)


def scene_record(facts: dict, path: Path) -> Path:
    """Where the all-time scoreboard stands once this week is folded in."""
    w, h = _wh()
    t = R._theme()
    fig, ax = R._canvas(w, h)
    _lheader(ax, w, "the record so far", facts["week_number"])

    acc = facts.get("alltime_accuracy_pct")
    R._text(ax, 360, 420, f"{acc:.0f}%" if acc is not None else "—", size=190,
            weight="bold", ha="center",
            color=t["up"] if acc is not None and acc >= 50 else t["down"])
    R._text(ax, 360, 560, f"{facts.get('alltime_hits', 0)} hits · "
                          f"{facts.get('alltime_misses', 0)} misses", size=34,
            color=t["muted"], ha="center")
    R._text(ax, 360, 620, f"over {facts.get('alltime_resolved', 0)} graded calls",
            size=34, color=t["muted"], ha="center")

    # Every graded outcome as a dot strip — the honest shape of the record,
    # which an aggregate percentage always flatters or hides.
    recent = facts.get("_recent") or []
    if recent:
        R._text(ax, 120, 760, f"LAST {len(recent)} CALLS", size=28, weight="bold",
                color=t["muted"])
        dot, dgap = 34, 12
        x = 120
        for r in recent:
            ax.add_patch(R.plt.Circle((x + dot / 2, 850), dot / 2,
                                      color=t["up"] if r["correct"] else t["down"],
                                      zorder=3))
            x += dot + dgap

    streak_n = facts.get("streak_length") or 0
    a30 = facts.get("alltime_accuracy_30d_pct")
    by = facts.get("by_signal") or {}
    rows_txt = [("Last 30 days", f"{a30:.0f}%" if a30 is not None else "—"),
                ("Current streak", f"{streak_n} "
                                   f"{'hits' if facts.get('streak_kind') == 'hit' else 'misses'}")]
    for sig in ("BUY", "HOLD", "SELL"):
        s = by.get(sig) or {}
        a = s.get("accuracy_pct")
        rows_txt.append((f"{sig} calls",
                         f"{a:.0f}% of {s.get('n', 0)}" if a is not None
                         else f"— of {s.get('n', 0)}"))

    y = 300
    for label, value in rows_txt:
        R._panel(ax, w - 780, y, 660, 118, color=t["panel"])
        R._text(ax, w - 740, y + 59, label, size=34, color=t["muted"])
        R._text(ax, w - 160, y + 59, value, size=40, weight="bold", ha="right")
        y += 136

    R._text(ax, w / 2, h - 70, "the full log is public at quantaura.tech",
            size=28, color=t["muted"], ha="center")
    return R._save(fig, path)


def scene_method(week_no: int, path: Path, steps_shown: int | None = None) -> Path:
    """How a call is made and graded — the card that makes every other number
    in the video checkable. Fixed content, guarded by `_assert_method_matches_config`."""
    w, h = _wh()
    t = R._theme()
    fig, ax = R._canvas(w, h)
    _lheader(ax, w, "how a call is graded", week_no)

    n = steps_shown if steps_shown is not None else len(METHOD_STEPS)
    bw, gap = 400, 30
    total = len(METHOD_STEPS) * bw + (len(METHOD_STEPS) - 1) * gap
    x = (w - total) / 2
    for i, (title, sub) in enumerate(METHOD_STEPS):
        if i >= n:
            break
        R._panel(ax, x, 330, bw, 400, color=t["panel"])
        R._text(ax, x + bw / 2, 405, str(i + 1), size=64, weight="bold",
                color=t["accent"], ha="center")
        # Wrapped by character count, then measured: bold caps at this size run
        # far wider than the count suggests, and a title that overflows its own
        # panel runs straight into the next card's.
        for j, line in enumerate(R._wrap(title, 12)[:2]):
            R._fit_text(fig, ax, 495 + j * 50, line, 34, max_w=bw - 56,
                        x=x + bw / 2, weight="bold")
        for j, line in enumerate(R._wrap(sub, 20)[:3]):
            R._fit_text(fig, ax, 610 + j * 42, line, 26, max_w=bw - 56,
                        x=x + bw / 2, min_size=18, color=t["muted"])
        x += bw + gap

    R._text(ax, w / 2, h - 150, "The same rule every day, applied to a call that was "
            "already written down.", size=34, ha="center")
    R._text(ax, w / 2, h - 80, "You can check any of it on your own chart.",
            size=30, color=t["muted"], ha="center")
    return R._save(fig, path)


def scene_research(segment: tuple[str, list[str], list[str]], week_no: int,
                   path: Path, bullets_shown: int | None = None) -> Path:
    w, h = _wh()
    t = R._theme()
    fig, ax = R._canvas(w, h)
    _lheader(ax, w, "inside the research", week_no)

    title, bullets, _ = segment
    R._fit_text(fig, ax, 360, title, 72, max_w=w - 240, x=w / 2, weight="bold")
    y = 520
    for b in bullets[:bullets_shown if bullets_shown is not None else len(bullets)]:
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
    pairs = [("method", s) for s in METHOD_SENTENCES]
    pairs += [("research", s) for s in segment[2]]
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

def _sentence_cuts(narration: V.Narration, section: str, n_stages: int) -> list[float]:
    """Stage boundaries at sentence starts, measured from the section's start.

    A card that arrives while the sentence introducing it is being spoken reads
    as illustration. The same card arriving three seconds into a forty-second
    scene reads as decoration, which is what a fixed front-loaded deal produces
    once a section runs long.

    Returns fewer boundaries than requested when the section has fewer
    sentences than stages; `_staged_clip` drops the surplus stages rather than
    inventing beats for them.
    """
    starts = [s.start for s in narration.segments if s.section == section]
    if len(starts) < 2 or n_stages < 2:
        return []
    base, rest = starts[0], starts[1:]
    if n_stages - 1 > len(rest):
        return [t - base for t in rest]
    step = len(rest) / (n_stages - 1)
    idx = sorted({min(len(rest) - 1, int(round(i * step))) for i in range(n_stages - 1)})
    return [rest[i] - base for i in idx]


def _staged_clip(pngs: list[Path], total: float, out: Path, w: int, h: int,
                 cuts: list[float] | None = None) -> Path:
    """A scene that builds itself in: each still held briefly, the last one for
    the rest of the section.

    One still clip per stage rather than a 30fps frame sequence — a reveal has
    only as many distinct images as it has stages, and re-rendering a 1920x1080
    matplotlib figure thirty times a second to show seven of them would cost
    minutes per scene for no visible difference.

    No Ken Burns drift here. The drift pans across a slightly oversized still,
    so restarting it at every stage would pop the scale on each step; the reveal
    is the motion this scene needs.
    """
    if cuts:
        cuts = sorted(t for t in cuts if 0 < t < total)
        pngs = pngs[:len(cuts) + 1]
    n = len(pngs)
    if n == 1 or total < 0.3 * n:
        return R._still_clip(pngs[-1], total, out, drift=False, w=w, h=h)
    if not cuts:
        # No narration beats to hang the stages on (the tabular scenes, whose
        # rows outnumber their sentences): deal evenly across the first 60% and
        # leave the finished card up for the rest.
        cuts = [total * 0.6 * (i + 1) / (n - 1) for i in range(n - 1)]

    # Boundaries are clamped forward so they stay ordered and the final stage
    # always ends exactly at `total`. The sum has to equal the section length
    # to the frame: _crossfade centres each transition on a running boundary,
    # and a scene that is even slightly long walks the picture off the voice.
    bounds = [0.0]
    for c in cuts:
        bounds.append(min(max(c, bounds[-1] + 0.25), total - 0.25 * (n - len(bounds))))
    bounds.append(total)

    parts = []
    for i, png in enumerate(pngs):
        parts.append(R._still_clip(png, bounds[i + 1] - bounds[i],
                                   FRAMES_DIR / f"{out.stem}_s{i}.mp4",
                                   drift=False, w=w, h=h))
    return R._concat(parts, out)


def _stage_pngs(facts: dict, segment, wk: int) -> dict[str, list[Path]]:
    """Every scene's stills, keyed by section. A single-element list renders as
    a plain still; more than one becomes a staged reveal."""
    days = facts["days"]
    curve = facts.get("_portfolio_curve") or []
    port_steps = min(5, max(len(curve), 1))
    return {
        "intro": [scene_title(facts, FRAMES_DIR / "title.png")],
        "days": [scene_days(facts, FRAMES_DIR / f"days_{i}.png", rows_shown=i + 1)
                 for i in range(len(days))],
        "spotlight": [scene_spotlight(facts, FRAMES_DIR / "spotlight.png")],
        "trend": [scene_trend(facts, facts.get("_candles"), FRAMES_DIR / "trend.png")],
        "portfolio": [scene_portfolio(facts, curve, FRAMES_DIR / f"port_{i}.png",
                                      reveal=(i + 1) / port_steps)
                      for i in range(port_steps)],
        "record": [scene_record(facts, FRAMES_DIR / "record.png")],
        "method": [scene_method(wk, FRAMES_DIR / f"method_{i}.png", steps_shown=i + 1)
                   for i in range(len(METHOD_STEPS))],
        # Starts on the title alone, then one bullet per sentence — the first
        # research sentence is a lead-in that introduces no bullet of its own.
        "research": [scene_research(segment, wk, FRAMES_DIR / f"research_{i}.png",
                                    bullets_shown=i)
                     for i in range(len(segment[1]) + 1)],
        "watchlist": [scene_watchlist(wk, FRAMES_DIR / "watchlist.png")],
        "outro": [scene_outro_wide(wk, FRAMES_DIR / "outro.png")],
    }


def assemble(facts: dict, script: dict, candles: list[list] | None,
             narration: V.Narration, segment, sections: list[str]) -> Path:
    w, h = _wh()
    wk = facts["week_number"]
    cfg = config()["video"]
    xf = float(cfg["transition_seconds"])

    if FRAMES_DIR.exists():
        shutil.rmtree(FRAMES_DIR)
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)

    facts["_candles"] = candles
    spans = narration.section_spans(sections)
    stages = _stage_pngs(facts, segment, wk)

    # The fixed segments have one still per spoken sentence, so their stages
    # can land on the narration. The tabular scenes have more rows than
    # sentences and deal on their own clock instead — see `_staged_clip`.
    SENTENCE_PACED = ("method", "research")

    clips, durations = [], []
    for i, sec in enumerate(sections):
        d = max(spans.get(sec, 0.0), 2.0)
        pngs = stages[sec]
        out = BUILD_DIR / f"wk_clip_{i}_{sec}.mp4"
        if len(pngs) == 1:
            clips.append(R._still_clip(pngs[0], d + xf, out,
                                       drift=(sec != "outro"), w=w, h=h))
        else:
            cuts = (_sentence_cuts(narration, sec, len(pngs))
                    if sec in SENTENCE_PACED else None)
            clips.append(_staged_clip(pngs, d + xf, out, w, h, cuts=cuts))
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


def _dry_script(facts: dict, required: list[str]) -> dict:
    """Fixture narration, sized like the real thing.

    Deliberately close to the middle of `word_budget` rather than a token
    sentence per section: the dry run's job is to show what the finished recap
    looks like, and a 50-word script would render six-second scenes and hide
    every layout problem a four-minute video actually has.
    """
    sections = {
        "intro": ["Five of seven calls landed this week, the model's best run so far.",
                  "The week ran from Monday to Sunday, and the misses both came "
                  "from directional calls rather than holds.",
                  "Here is every one of them, graded the same way, including the "
                  "two the model got wrong."],
        "days": ["The week opened with two holds, both correct, in a market that "
                 "refused to move more than a fraction of a percent.",
                 "Wednesday was the first directional call of the week, a buy, and "
                 "the market delivered a rise well outside the flat band.",
                 "Thursday was the miss that stung. The model held through a move "
                 "that cleared the band comfortably.",
                 "Friday went wrong in the other direction, a buy into a fall.",
                 "The weekend brought the model back, a hold and a buy, both graded "
                 "correct."],
        "spotlight": ["Friday is the call worth sitting with, because it is the one "
                      "that cost the most.",
                      "The model called a buy with real conviction and the market "
                      "fell almost two percent against it.",
                      "A confident wrong call is worse than an unconfident one, and "
                      "the scoreboard does not soften either.",
                      "It stays in the log exactly as it was written, which is the "
                      "only thing that makes the rest of these numbers worth "
                      "anything."],
        "trend": ["Across the whole week the price moved in a narrow range with two "
                  "sharp days at either end of it.",
                  "Every call sits on that line at the moment it was made, with a "
                  "ring showing how it graded.",
                  "Seen that way the pattern is easy to read. The model handled the "
                  "quiet middle of the week and lost money at both edges."],
        "portfolio": ["The paper portfolio traded mechanically on those same calls, "
                      "starting from ten thousand dollars.",
                      "It ended the period ahead of where it started, but that is "
                      "only half the comparison that matters.",
                      "Buy and hold is the other half, and the fees are counted "
                      "against the strategy on every position change."],
        "record": ["All time, the record now stands at five correct out of seven "
                   "graded calls.",
                   "Holds carry most of that number, which is what a confidence "
                   "gate is supposed to produce.",
                   "The sample is still far too small to draw a conclusion from, "
                   "and saying so is part of the job.",
                   "Ask again after a few hundred graded calls. Until then the "
                   "number on this card is a running total, not a result."],
    }
    return {
        "title": f"BTC Model Week {facts['week_number']}: "
                 f"{facts['week_hits']} of {facts['week_resolved']} Correct — dry run",
        "description": "Dry-run build of the weekly recap. Not uploaded.",
        "tags": ["bitcoin", "machine learning"],
        "sections": {s: sections[s] for s in required},
        "word_count": sum(len(x.split()) for s in required for x in sections[s]),
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

def _prior_weekly() -> tuple[dict, dict, Path] | None:
    """Facts, script and mp4 of a recap that already published, if the workflow
    restored its artifact into build/. See pipeline._prior_build — same idea,
    and the same reason: mirroring a re-render would put a different video on X
    from the one on YouTube."""
    from common import read_json
    video = BUILD_DIR / "weekly.mp4"
    facts_p, script_p = (BUILD_DIR / "weekly_facts.json",
                         BUILD_DIR / "weekly_script.json")
    if not (video.exists() and facts_p.exists() and script_p.exists()):
        return None
    facts, script = read_json(facts_p), read_json(script_p)
    if not (facts and script and facts.get("week_number")):
        return None
    return facts, script, video


def run(args: argparse.Namespace) -> int:
    stage = "startup"
    now = datetime.now(timezone.utc)
    try:
        publishing = not (args.dry_run or args.no_upload or args.drive_only)
        if publishing and not args.force and published_today("weekly"):
            log.info("A weekly recap was already published today — nothing to do.")
            return 0

        # ── reuse the published recap, when the workflow restored one ──
        # A recap takes minutes to render and its blog post is already live, so
        # re-deriving it to produce an X clip would be both slow and wrong.
        if args.drive_only:
            prior = _prior_weekly()
            if prior:
                stage = "drive"
                facts, script, video = prior
                log.info(f"Reusing the published recap for week "
                         f"{facts.get('week_number')} — same file as the video.")
                drive_info = drive_mod.mirror_weekly(video, facts)
                ok = bool(drive_info and drive_info.get("ok"))
                record_run("mirrored" if ok else "rendered", "drive",
                           "Mirrored the published recap — no re-render, no upload.",
                           {"video": str(video), "reused": True,
                            **drive_mod.run_log_extra(drive_info)}, kind="weekly")
                notify.rendered_only({}, script, str(video),
                                     "--drive-only (mirrored the published recap)",
                                     drive_info)
                return 0 if ok else 1
            log.info("No published recap restored into build/ — rendering fresh.")

        _assert_method_matches_config()

        stage = "collect"
        if args.dry_run:
            log.info("DRY RUN — synthetic week, no live calls")
            rows = _dry_rows(now)
            summary = {"resolved_calls": 7, "hits": 5, "misses": 2,
                       "accuracy_pct": 71.4, "accuracy_30d_pct": 71.4,
                       "resolved_30d": 7, "streak": {"kind": "hit", "length": 2},
                       "by_signal": {"BUY": {"n": 3, "accuracy_pct": 66.7},
                                     "HOLD": {"n": 4, "accuracy_pct": 75.0},
                                     "SELL": {"n": 0, "accuracy_pct": None}},
                       "flat_band_pct": 1.0, "horizon_hours": 24,
                       # Real arithmetic over the fixture rows rather than a
                       # hand-written curve — the portfolio scene is then
                       # exercised by the same code that produces the live one.
                       "portfolio": scoreboard._portfolio(rows)}
            candles = None
        else:
            if args.drive_only:
                # Read, never write. Grading is the scheduled run's job; a
                # --drive-only pass exists to produce a clip, not to move the
                # record.
                log.info("  --drive-only: reading the scoreboard, not grading")
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
        sections = active_sections(facts)
        required = [s for s in LLM_SECTIONS if s in sections]
        if len(required) < len(LLM_SECTIONS):
            log.warning(f"  Dropping section(s) the facts cannot support: "
                        f"{', '.join(s for s in LLM_SECTIONS if s not in required)}")
        write_json(BUILD_DIR / "weekly_facts.json", facts)

        stage = "script"
        script = (_dry_script(facts, required) if args.dry_run
                  else write_script(facts, required))
        write_json(BUILD_DIR / "weekly_script.json", script)

        stage = "voice"
        segment = RESEARCH_SEGMENTS[facts["week_number"] % len(RESEARCH_SEGMENTS)]
        sentences = [(sec, s) for sec in required for s in script["sections"][sec]]
        sentences += fixed_sentences(segment)
        narration = V.synthesize(sentences, out_name="weekly_narration.wav",
                                 silent=args.no_voice or args.dry_run)

        stage = "render"
        # Underscored keys are attached only now, after the fact-check has run.
        # Everything reachable from `facts` widens factcheck's allow-list of
        # speakable numbers, and these three are bulk data the narration must
        # never quote from — see build_facts on the portfolio curve.
        facts["_rows"] = rows           # scene_trend needs made_at_ms + price
        facts["_portfolio_curve"] = (summary.get("portfolio") or {}).get("curve") or []
        facts["_recent"] = (rows if args.dry_run else
                            [r for r in scoreboard._load() if r.get("resolved")][-20:])
        video = assemble(facts, script, candles, narration, segment, sections)
        duration = R.duration_of(video)
        log.info(f"  Weekly video: {duration:.1f}s" if duration else "  Weekly video ready")

        cap = int(config()["weekly"]["max_seconds"])
        if duration and duration > cap:
            # A warning, not an abort: an over-long recap is still a publishable
            # video, and discarding a finished render over pacing would cost the
            # week's upload for a fixable prompt problem.
            log.warning(f"  Recap ran {duration:.0f}s, over the {cap}s target — "
                        f"tighten word_budget() if this repeats.")

        stage = "thumbnail"
        import thumbnail
        thumb = thumbnail.build_weekly(facts)

        # ── mirror to Drive ──
        # Ahead of the upload, same reasoning as the daily: X is hand-delivered
        # and has no retry of its own. Note the recap is landscape and minutes
        # long, so it exceeds X's 140s cap for non-Premium accounts — it is
        # mirrored for trimming or for posting elsewhere, not to go up whole.
        stage = "drive"
        drive_info = None
        if args.no_drive or args.no_upload or args.dry_run:
            log.info("Skipping the Drive mirror.")
        else:
            drive_info = drive_mod.mirror_weekly(video, facts)

        stage = "upload"
        if args.drive_only:
            # No blog: the recap's written post went out with the scheduled run
            # and republishing it would duplicate a live page for the sake of a
            # video clip.
            ok = bool(drive_info and drive_info.get("ok"))
            log.info("--drive-only: skipping YouTube and the blog post." if ok
                     else "--drive-only: Drive mirror failed, nothing to show for it.")
            record_run("mirrored" if ok else "rendered", "drive",
                       "Rendered and mirrored — no upload, no blog post.",
                       {"video": str(video), "duration_s": round(duration or 0, 1),
                        **drive_mod.run_log_extra(drive_info)}, kind="weekly")
            notify.rendered_only({}, script, str(video),
                                 "--drive-only (X clip requested on demand)",
                                 drive_info)
            return 0 if ok else 1

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
            record_run("rendered", "upload", str(e),
                       {"video": str(video), **drive_mod.run_log_extra(drive_info)},
                       kind="weekly")
            notify.rendered_only({}, script, str(video), str(e), drive_info)
            return 0

        _publish_blog(facts, script, summary, segment, url, False)
        record_run("published", "upload", script["title"],
                   {"url": url, "visibility": config()["upload"]["visibility"],
                    "duration_s": round(duration or 0, 1),
                    "run_id": os.environ.get("GITHUB_RUN_ID"),
                    **drive_mod.run_log_extra(drive_info)}, kind="weekly")
        notify.weekly_published(script["title"], url, facts,
                                config()["upload"]["visibility"], drive_info)
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
    p.add_argument("--no-drive", action="store_true",
                   help="skip the Google Drive mirror (still uploads to YouTube)")
    p.add_argument("--drive-only", action="store_true",
                   help="mirror to Drive for an X clip, without uploading to "
                        "YouTube, publishing the blog post, or grading")
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
