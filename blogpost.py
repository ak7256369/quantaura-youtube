"""The weekly recap as a written post — the channel's content, made crawlable.

Every fact here is either a committed scoreboard row, a number the pipeline
computed, or narration that already passed the fact-check gates for the video.
Nothing is written for the blog that was not already verified for YouTube —
the post is a second rendering of the same reviewed material, not a second
author.

Posts are JSON, not HTML: the site owns presentation, this repo owns facts.
They live in state/blog/ and commit with the rest of the run's state; the
site reads them through the product API. `index.json` is derived by scanning
the post files, so it can always be rebuilt and never disagrees with them.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from common import BUILD_DIR, STATE_DIR, config, log, write_json

BLOG_DIR = STATE_DIR / "blog"


def _slug(iso_year: int, iso_week: int) -> str:
    return f"bitcoin-model-week-{iso_year}-{iso_week:02d}"


def _title(facts: dict) -> str:
    """Deterministic headline. The video title is written for a YouTube feed;
    a blog headline should carry the keyword and the record on its own."""
    return (f"Bitcoin AI Model Review — Week {facts['week_number']}: "
            f"{facts['week_hits']} of {facts['week_resolved']} Calls Correct")


def _meta_description(facts: dict, portfolio: dict | None) -> str:
    """The <meta description>: numbers first, honestly, within 160 chars.

    Built clause by clause and never sliced — a description cut mid-word
    reads as broken in a search snippet, which defeats its entire purpose.
    """
    acc = facts.get("week_accuracy_pct")
    desc = (f"Week {facts['week_number']} graded: {facts['week_hits']} of "
            f"{facts['week_resolved']} Bitcoin calls correct"
            + (f" ({acc:.0f}%)" if acc is not None else "")
            + f". All-time: {facts.get('alltime_accuracy_pct')}% over "
              f"{facts.get('alltime_resolved')} public calls.")
    for clause in ([f" Paper portfolio {portfolio['return_pct']:+.1f}% vs holding."]
                   if portfolio else []) + [" Logged before outcomes were knowable."]:
        if len(desc) + len(clause) <= 160:
            desc += clause
    return desc


def build_post(facts: dict, script: dict, summary: dict,
               segment: tuple[str, list[str], list[str]],
               video_url: str | None) -> dict:
    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    portfolio = summary.get("portfolio") or None
    res_title, res_bullets, res_sentences = segment

    return {
        "slug": _slug(iso.year, facts["week_number"]),
        "iso_year": iso.year,
        "iso_week": facts["week_number"],
        "title": _title(facts),
        "meta_description": _meta_description(facts, portfolio),
        "published_at": now.isoformat(timespec="seconds"),
        "updated_at": now.isoformat(timespec="seconds"),
        "date_range": facts.get("date_range", ""),

        "week": {
            "resolved": facts["week_resolved"],
            "hits": facts["week_hits"],
            "misses": facts["week_misses"],
            "accuracy_pct": facts.get("week_accuracy_pct"),
            "days": facts["days"],
        },
        "alltime": {
            "resolved": facts.get("alltime_resolved"),
            "hits": facts.get("alltime_hits"),
            "misses": facts.get("alltime_misses"),
            "accuracy_pct": facts.get("alltime_accuracy_pct"),
        },
        "portfolio": ({
            "value_usd": portfolio["value_usd"],
            "return_pct": portfolio["return_pct"],
            "hold_return_pct": portfolio["hold_return_pct"],
            "vs_hold_pct": portfolio["vs_hold_pct"],
            "fees_paid_usd": portfolio["fees_paid_usd"],
            "trades": portfolio["trades"],
            "position": portfolio["position"],
        } if portfolio else None),

        # Narration that already passed the video's fact-check gates. Keyed off
        # the script's own sections rather than a hard-coded list, so a section
        # added to (or dropped from) the recap reaches the post without a second
        # edit here — weekly.LLM_SECTIONS is the single source of that order.
        "narrative": {sec: list(sentences)
                      for sec, sentences in script["sections"].items()},
        "research": {
            "title": res_title,
            "bullets": list(res_bullets),
            # Drop the spoken lead-in ("A quick look inside...") — it is a
            # video transition, not prose.
            "paragraphs": [s for s in res_sentences
                           if not s.startswith("A quick look")],
        },
        "scoring": {
            "horizon_hours": facts.get("horizon_hours"),
            "flat_band_pct": facts.get("flat_band_pct"),
        },
        "video_url": video_url,
    }


def _rebuild_index(blog_dir: Path) -> dict:
    """Scan the post files and derive the index — never hand-maintained."""
    posts = []
    for f in sorted(blog_dir.glob("*.json")):
        if f.name == "index.json":
            continue
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:                                   # noqa: BLE001
            log.warning(f"  Skipping unreadable post {f.name}: {e}")
            continue
        posts.append({
            "slug": p["slug"],
            "title": p["title"],
            "meta_description": p["meta_description"],
            "published_at": p["published_at"],
            "updated_at": p.get("updated_at", p["published_at"]),
            "iso_year": p["iso_year"],
            "iso_week": p["iso_week"],
            "week_resolved": p["week"]["resolved"],
            "week_hits": p["week"]["hits"],
            "week_accuracy_pct": p["week"]["accuracy_pct"],
        })
    posts.sort(key=lambda p: p["published_at"], reverse=True)
    return {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "posts": posts}


def publish(facts: dict, script: dict, summary: dict,
            segment: tuple[str, list[str], list[str]],
            video_url: str | None, dry_run: bool = False) -> Path:
    """Write the week's post and rebuild the index. Returns the post path.

    A re-run in the same ISO week overwrites its own post (same slug) — the
    post is derived from the log, so regenerating it is idempotent, and
    `updated_at` records that it happened. Dry runs write to build/ so the
    committed blog can never contain fixture numbers.
    """
    post = build_post(facts, script, summary, segment, video_url)
    out_dir = (BUILD_DIR if dry_run else BLOG_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    post_path = out_dir / f"{post['slug']}.json"
    if post_path.exists():
        try:
            prev = json.loads(post_path.read_text(encoding="utf-8"))
            post["published_at"] = prev.get("published_at", post["published_at"])
        except Exception:                                        # noqa: BLE001
            pass
    write_json(post_path, post)
    log.info(f"  Blog post: {post_path.name}"
             + (" (dry run — build/ only)" if dry_run else ""))

    if not dry_run:
        write_json(BLOG_DIR / "index.json", _rebuild_index(BLOG_DIR))
    return post_path
