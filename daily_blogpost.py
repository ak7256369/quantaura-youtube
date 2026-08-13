"""Daily prediction post — one post per pipeline run, automatically crawlable.

Every fact here is either a committed scoreboard row, a live snapshot value,
or narration that already passed the fact-check gates for the daily video.
Nothing is written for the blog that was not already verified for the video.

Posts are JSON, not HTML: the site owns presentation, this repo owns facts.
They live in state/blog/daily/ and commit with the rest of the run's state.
The API merges these with the weekly posts and serves them together.

Slug format: bitcoin-daily-YYYY-MM-DD
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from common import BUILD_DIR, STATE_DIR, config, log, write_json

DAILY_BLOG_DIR = STATE_DIR / "blog" / "daily"


def _slug(date: str) -> str:
    """date is a YYYY-MM-DD string from the snapshot."""
    return f"bitcoin-daily-{date}"


def _signal_label(signal: str, raw: str, gated: bool) -> str:
    """Human-readable signal label for the title."""
    if gated:
        return f"{signal} (gated from {raw})"
    return signal


def _title(snapshot: dict) -> str:
    signal = snapshot.get("signal", "HOLD")
    raw = snapshot.get("signal_raw", signal)
    gated = snapshot.get("gated", False)
    confidence = snapshot.get("confidence", 0)
    date = snapshot.get("date", "")
    label = _signal_label(signal, raw, gated)
    return (f"Bitcoin AI Call — {date}: {label} at {confidence:.0f}% Confidence")


def _meta_description(snapshot: dict, score: dict) -> str:
    signal = snapshot.get("signal", "HOLD")
    confidence = snapshot.get("confidence", 0)
    date = snapshot.get("date", "")
    price = snapshot.get("price_str", "")
    acc = score.get("accuracy_pct")
    resolved = score.get("resolved_calls", 0)

    desc = (
        f"Bitcoin AI model call for {date}: {signal} at {confidence:.0f}% confidence. "
        f"BTC trading at {price}."
    )
    suffix = f" All-time record: {acc:.0f}% over {resolved} public calls." if acc else ""
    if len(desc) + len(suffix) <= 160:
        desc += suffix
    return desc


def _narrative(snapshot: dict, script: dict, score: dict) -> dict:
    """Pull the already-verified narration from the daily script sections.

    The script sections are: hook, call, why, score.
    We map them into intro/days/trend so the blog schema stays consistent.
    """
    sections = script.get("sections", {})
    return {
        "intro": list(sections.get("hook", [])) + list(sections.get("call", [])),
        "days":  list(sections.get("why", [])),
        "trend": list(sections.get("score", [])),
    }


def build_post(snapshot: dict, script: dict, score: dict,
               video_url: str | None) -> dict:
    now = datetime.now(timezone.utc)
    date = snapshot.get("date", now.strftime("%Y-%m-%d"))

    # Per-model breakdown and weights for the model-vote card.
    per_model = snapshot.get("per_model") or {}
    weights = snapshot.get("weights") or {}

    # Fear & Greed index.
    fg = snapshot.get("fear_greed")
    fear_greed = ({"value": fg["value"], "classification": fg["classification"]}
                  if fg and fg.get("value") is not None else None)

    # Resolved outcome for today's call (None if not yet graded — it is
    # graded 24 h later by resolve_due() in the next pipeline run).
    change_pct = snapshot.get("change_24h_pct")

    # All-time record from the scoreboard summary.
    alltime = {
        "resolved": score.get("resolved_calls"),
        "hits":     score.get("hits"),
        "misses":   score.get("misses"),
        "accuracy_pct": score.get("accuracy_pct"),
    }

    return {
        "slug": _slug(date),
        "post_type": "daily",
        "iso_year": now.year,
        "iso_week": now.isocalendar().week,
        "title": _title(snapshot),
        "meta_description": _meta_description(snapshot, score),
        "published_at": now.isoformat(timespec="seconds"),
        "updated_at": now.isoformat(timespec="seconds"),
        "date_range": date,

        # Top-level signal fields (displayed in the signal card).
        "signal": snapshot.get("signal", "HOLD"),
        "signal_raw": snapshot.get("signal_raw"),
        "confidence_pct": snapshot.get("confidence"),
        "gated": snapshot.get("gated", False),
        "change_pct": change_pct,
        "per_model": per_model,
        "weights": weights,
        "fear_greed": fear_greed,

        # Narration from the daily script (already fact-checked for the video).
        "narrative": _narrative(snapshot, script, score),

        # Stub week/alltime/portfolio/research/scoring blocks so the TypeScript
        # interface stays satisfied — the site renders these based on post_type.
        "week": {
            "resolved": 0, "hits": 0, "misses": 0, "accuracy_pct": None,
            "days": [],
        },
        "alltime": alltime,
        "portfolio": None,
        "research": {"title": "", "bullets": [], "paragraphs": []},
        "scoring": {
            "horizon_hours": snapshot.get("label_horizon_hours", 24),
            "flat_band_pct": config().get("scoring", {}).get("flat_band_pct", 1.0),
        },
        "video_url": video_url,
    }


def _rebuild_index(daily_dir: Path) -> dict:
    """Scan post files and rebuild the daily index — never hand-maintained."""
    posts = []
    for f in sorted(daily_dir.glob("*.json")):
        if f.name == "index.json":
            continue
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:                                   # noqa: BLE001
            log.warning(f"  Skipping unreadable daily post {f.name}: {e}")
            continue
        posts.append({
            "slug": p["slug"],
            "post_type": "daily",
            "title": p["title"],
            "meta_description": p["meta_description"],
            "published_at": p["published_at"],
            "updated_at": p.get("updated_at", p["published_at"]),
            "iso_year": p["iso_year"],
            "iso_week": p["iso_week"],
            # week_resolved/hits/accuracy_pct are 0/None for daily posts;
            # the frontend uses post_type to decide what to render.
            "week_resolved": 1 if p.get("change_pct") is not None else 0,
            "week_hits": 0,
            "week_accuracy_pct": None,
        })
    posts.sort(key=lambda p: p["published_at"], reverse=True)
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "posts": posts,
    }


def publish(snapshot: dict, script: dict, score: dict,
            video_url: str | None, dry_run: bool = False) -> Path:
    """Write today's daily post and rebuild the daily index.

    A re-run on the same date overwrites its own post (same slug) — the post
    is derived from the pipeline's outputs, so regenerating it is idempotent,
    and `updated_at` records that it happened. Dry runs write to build/ so the
    committed blog can never contain fixture numbers.
    """
    post = build_post(snapshot, script, score, video_url)
    out_dir = (BUILD_DIR if dry_run else DAILY_BLOG_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    post_path = out_dir / f"{post['slug']}.json"
    if post_path.exists():
        try:
            prev = json.loads(post_path.read_text(encoding="utf-8"))
            post["published_at"] = prev.get("published_at", post["published_at"])
        except Exception:                                        # noqa: BLE001
            pass
    write_json(post_path, post)
    log.info(f"  Daily blog post: {post_path.name}"
             + (" (dry run — build/ only)" if dry_run else ""))

    if not dry_run:
        write_json(DAILY_BLOG_DIR / "index.json", _rebuild_index(DAILY_BLOG_DIR))
    return post_path
