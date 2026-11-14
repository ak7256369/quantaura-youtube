"""Rebuild missing daily blog posts from the committed prediction log.

From 2026-09-29 the YouTube OAuth token was revoked, and pipeline.py only wrote
the daily post after a successful upload — so the blog went silent while the
model kept making (and the scoreboard kept grading) a call every day.

Every number in a backfilled post comes from state/predictions.jsonl: the call
as it was committed on the day, and its 24h grade. Nothing is re-predicted —
re-running today's models on old dates would be look-ahead. Days with no
committed call (the 2026-11-05..09 fetch outage) get no post.

The narration is templated from those fields rather than LLM-written, and each
post says it was rebuilt from the log and has no video.

Usage:  python backfill_blog.py --from 2026-09-29 --to 2026-11-13 [--dry-run]
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone

from common import STATE_DIR, log, write_json
from daily_blogpost import DAILY_BLOG_DIR, _rebuild_index, _slug

GATE_PCT = 55


def _load_predictions() -> list[dict]:
    rows = []
    with open(STATE_DIR / "predictions.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _record_before(rows: list[dict], day: str, made_at_ms: int | None = None) -> dict:
    """Public record as it stood when `day`'s call was made: every call from an
    earlier date whose 24h grade had landed."""
    graded = [r for r in rows if r["date"] < day and r.get("resolved")
              and r.get("correct") is not None]
    hits = sum(1 for r in graded if r["correct"])
    n = len(graded)
    # Same window as scoreboard.summary(): calls made in the 30 days before
    # the run, by made_at_ms.
    if made_at_ms is None:
        made_at_ms = int(datetime.fromisoformat(f"{day}T23:00:00+00:00").timestamp() * 1000)
    cutoff = made_at_ms - 30 * 86_400_000
    recent = [r for r in graded if r.get("made_at_ms", 0) >= cutoff]
    return {
        "resolved": n, "hits": hits, "misses": n - hits,
        "accuracy_pct": round(100 * hits / n, 1) if n else None,
        "acc30": round(100 * sum(r["correct"] for r in recent) / len(recent), 1)
                 if recent else None,
    }


def _human_day(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d:%B} {d.day}, {d.year}"


def build_backfill_post(p: dict, rec: dict, now: datetime) -> dict:
    day = p["date"]
    sig, raw = p["signal"], p.get("signal_raw") or p["signal"]
    conf = p.get("confidence") or 0.0
    gated = bool(p.get("gated"))
    band = p.get("flat_band_pct", 1.0)
    price = p.get("price")

    label = f"{sig} (gated from {raw})" if gated else sig
    title = f"Bitcoin AI Call — {day}: {label} at {conf:.0f}% Confidence"

    intro = [f"On {_human_day(day)} the QuantAura four-model ensemble published a "
             f"{sig} call on Bitcoin at {conf:.1f} percent confidence."]
    if gated:
        intro.append(f"The raw ensemble output was {raw}, but its calibrated "
                     f"confidence was under the {GATE_PCT} percent gate, so the "
                     f"published call was {sig}.")
    if price:
        intro.append(f"Bitcoin was trading at ${price:,.0f} when the call was made.")

    days = []
    chg = p.get("change_pct")
    if p.get("resolved") and chg is not None:
        verdict = "correct" if p.get("correct") else "incorrect"
        days.append(f"Twenty-four hours later the price had moved {chg:+.2f} percent, "
                    f"so the call was graded {verdict}.")
        if sig == "HOLD":
            days.append(f"A HOLD call counts as correct only when price stays "
                        f"within ±{band:g} percent over the 24-hour horizon.")
        else:
            days.append(f"A {sig} call needs price to move more than {band:g} percent "
                        f"in its direction within 24 hours to be graded correct.")
    else:
        days.append("This call had not been graded yet when the post was built.")

    trend = []
    if rec["resolved"]:
        trend.append(f"Going into the day, the public record stood at {rec['hits']} "
                     f"hits and {rec['misses']} misses over {rec['resolved']} graded "
                     f"calls, an accuracy of {rec['accuracy_pct']:.1f} percent.")
    if rec["acc30"] is not None:
        trend.append(f"Over the preceding thirty days accuracy was "
                     f"{rec['acc30']:.1f} percent.")
    trend.append("This post was rebuilt from the committed prediction log while the "
                 "daily video pipeline was offline, so there is no video for this day.")

    desc = (f"Bitcoin AI model call for {day}: {sig} at {conf:.0f}% confidence."
            + (f" BTC at ${price:,.0f}." if price else ""))
    if rec["accuracy_pct"] is not None:
        suffix = (f" Record: {rec['accuracy_pct']:.0f}% over "
                  f"{rec['resolved']} public calls.")
        if len(desc) + len(suffix) <= 160:
            desc += suffix

    made = p.get("made_at") or f"{day}T23:00:00+00:00"
    iso = date.fromisoformat(day).isocalendar()
    return {
        "slug": _slug(day),
        "post_type": "daily",
        "iso_year": iso.year,
        "iso_week": iso.week,
        "title": title,
        "meta_description": desc,
        "published_at": made,
        "updated_at": now.isoformat(timespec="seconds"),
        "date_range": day,
        "signal": sig,
        "signal_raw": raw,
        "confidence_pct": conf,
        "gated": gated,
        "change_pct": chg,
        "correct": p.get("correct"),
        "per_model": None,     # not stored per day in the prediction log
        "weights": None,
        "fear_greed": None,
        "narrative": {"intro": intro, "days": days, "trend": trend},
        "week": {"resolved": 0, "hits": 0, "misses": 0,
                 "accuracy_pct": None, "days": []},
        "alltime": {k: rec[k] for k in ("resolved", "hits", "misses", "accuracy_pct")},
        "portfolio": None,
        "research": {"title": "", "bullets": [], "paragraphs": []},
        "scoring": {"horizon_hours": p.get("horizon_hours", 24),
                    "flat_band_pct": band},
        "video_url": None,
        "backfilled": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace existing posts (never touches non-backfilled ones)")
    a = ap.parse_args()

    rows = _load_predictions()
    # One committed call per date; if a date was logged twice keep the first,
    # which is the one the scoreboard grades.
    by_day: dict[str, dict] = {}
    for r in rows:
        if r.get("symbol", "BTCUSDT") == "BTCUSDT":
            by_day.setdefault(r["date"], r)

    now = datetime.now(timezone.utc)
    DAILY_BLOG_DIR.mkdir(parents=True, exist_ok=True)
    wrote, skipped, missing = [], [], []
    d, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    while d <= end:
        day = d.isoformat()
        d += timedelta(days=1)
        p = by_day.get(day)
        if not p:
            missing.append(day)
            continue
        path = DAILY_BLOG_DIR / f"{_slug(day)}.json"
        if path.exists():
            prev = json.loads(path.read_text(encoding="utf-8"))
            if not (a.overwrite and prev.get("backfilled")):
                skipped.append(day)
                continue
        post = build_backfill_post(p, _record_before(rows, day, p.get("made_at_ms")), now)
        if a.dry_run:
            print(json.dumps(post, indent=1, ensure_ascii=False)[:1200])
        else:
            write_json(path, post)
        wrote.append(day)

    if not a.dry_run and wrote:
        write_json(DAILY_BLOG_DIR / "index.json", _rebuild_index(DAILY_BLOG_DIR))
    log.info(f"backfill: wrote {len(wrote)}, kept existing {len(skipped)}, "
             f"no committed call for {len(missing)}: {', '.join(missing) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
