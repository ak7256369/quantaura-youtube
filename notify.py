"""Operator notifications over Telegram.

The pipeline is unattended, so the only thing standing between a silent failure
and a dead channel is that every run — success, skip, or crash — reports itself.
A missing bot token degrades to a log line rather than an error: notification
is observability, not a dependency.
"""
from __future__ import annotations

import html

from common import env, log, post_json

API = "https://api.telegram.org/bot{token}/sendMessage"


def _send(text: str) -> bool:
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        log.info("  (Telegram not configured — notification skipped)")
        return False
    res = post_json(API.format(token=token),
                    {"chat_id": chat_id, "text": text[:4000],
                     "parse_mode": "HTML", "disable_web_page_preview": False},
                    retries=2, label="telegram")
    if not res.ok:
        log.warning(f"  Telegram notification failed: {res.error}")
    return res.ok


def _esc(v) -> str:
    return html.escape(str(v))


def _x_block(drive: dict | None) -> list[str]:
    """The manual-X-post section.

    X's API is pay-per-use since 2026-02-06 and the automated poster is off, so
    this notification *is* the workflow: tap the link, download, copy the
    caption, post. The caption goes in a <pre> block because Telegram renders
    those with a copy button — a caption you have to select by hand on a phone
    would not survive contact with a daily routine.
    """
    if drive is None:                    # never attempted — say nothing at all
        return []
    if not drive.get("ok"):
        return ["", "📥 <b>X post</b>: Drive mirror failed — grab the mp4 from "
                    "the workflow artifact if you want to post it.",
                f"<code>{_esc(drive.get('error', ''))[:200]}</code>"]
    lines = ["", "📥 <b>Ready to post on X</b>"]
    if drive.get("video_url"):
        lines.append(f"{drive['video_url']}  ({_esc(drive.get('size_mb'))} MB)")
    if drive.get("caption"):
        lines += ["", f"<pre>{_esc(drive['caption'])}</pre>"]
    return lines


def success(snapshot: dict, score: dict, script: dict, video_url: str | None,
            visibility: str, drive: dict | None = None) -> None:
    acc = score.get("accuracy_pct")
    lines = [
        "<b>✅ Video published</b>",
        f"<b>{_esc(script['title'])}</b>",
        "",
        f"Call: <b>{_esc(snapshot['signal'])}</b> @ {_esc(snapshot.get('confidence'))}%",
        f"Price: {_esc(snapshot['price_str'])} ({_esc(snapshot.get('change_24h_pct'))}% 24h)",
    ]
    if score.get("resolved_calls"):
        lines.append(f"Record: {_esc(score.get('hits'))}W/{_esc(score.get('misses'))}L "
                     f"· {_esc(acc)}% over {_esc(score.get('resolved_calls'))} calls")
    else:
        lines.append("Record: day one")
    lines.append("")
    if video_url:
        lines.append(f"🔗 {video_url}")
    lines.append(f"Visibility: <b>{_esc(visibility)}</b>"
                 + (" — review and set public" if visibility == "private" else ""))
    lines += _x_block(drive)
    _send("\n".join(lines))


def weekly_published(title: str, url: str, facts: dict, visibility: str,
                     drive: dict | None = None) -> None:
    _send("\n".join([
        "<b>📅 Weekly recap published</b>",
        f"<b>{_esc(title)}</b>",
        "",
        f"Week: {_esc(facts.get('week_hits'))}/{_esc(facts.get('week_resolved'))} correct",
        f"All-time: {_esc(facts.get('alltime_hits'))}/{_esc(facts.get('alltime_resolved'))} "
        f"({_esc(facts.get('alltime_accuracy_pct'))}%)",
        "",
        f"🔗 {url}",
        f"Visibility: <b>{_esc(visibility)}</b>"
        + (" — review and set public" if visibility == "private" else ""),
    ] + _x_block(drive)))


def rendered_only(snapshot: dict, script: dict, path: str, reason: str,
                  drive: dict | None = None) -> None:
    # "Not uploaded" means not on YouTube. The Drive mirror runs first and is
    # reported separately, so a failed YouTube upload can still leave a
    # perfectly good X post waiting.
    _send("\n".join([
        "<b>⚠️ Video rendered but not uploaded to YouTube</b>",
        f"<b>{_esc(script['title'])}</b>",
        "",
        f"Reason: {_esc(reason)}",
        f"File: <code>{_esc(path)}</code>",
        "",
        "The mp4 is attached to the workflow run as an artifact (7 days).",
    ] + _x_block(drive)))


def comments_digest(comments: list[dict], dry_run: bool = False):
    """New viewer comments, to the operator chat.

    Returns True when the digest was delivered (or there was nothing to send),
    False when it was not — comments.py leaves the comments unread in that
    case so the next run tries again. With dry_run it returns the rendered
    text instead of sending it.

    Built to stay under Telegram's 4096-character limit by dropping whole
    comments, never by slicing the string: a cut that lands inside a tag makes
    Telegram reject the entire message.
    """
    from common import config
    cap = int((config().get("comments") or {}).get("digest_max", 20))
    if not comments:
        return "" if dry_run else True

    head = f"💬 <b>{len(comments)} new comment{'s' if len(comments) != 1 else ''}</b>"
    blocks, used = [], len(head)
    for c in comments[:cap]:
        text = " ".join(str(c.get("text", "")).split())
        if len(text) > 400:
            text = text[:397] + "..."
        meta = [f"{_esc(c.get('video_kind') or 'video')} {_esc(c.get('video_date') or '')}".strip()]
        if c.get("likes"):
            meta.append(f"{int(c['likes'])} like{'s' if c['likes'] != 1 else ''}")
        if c.get("replies"):
            meta.append(f"{int(c['replies'])} repl{'ies' if c['replies'] != 1 else 'y'}")
        block = (f"<b>{_esc(c.get('author') or 'Someone')}</b> · {' · '.join(meta)}\n"
                 f"{_esc(text)}\n"
                 f"<a href=\"{_esc(c.get('url', ''))}\">Open on YouTube</a>")
        if used + len(block) + 2 > 3600:
            break
        blocks.append(block)
        used += len(block) + 2
    lines = [head, ""] + ["\n\n".join(blocks)]
    left = len(comments) - len(blocks)
    if left > 0:
        lines += ["", f"<i>+{left} more in YouTube Studio.</i>"]
    body = "\n".join(lines)
    if dry_run:
        return body
    return _send(body)


def skipped(reason: str, stage: str = "") -> None:
    _send("\n".join([
        "<b>🚫 No video today</b>",
        "",
        f"Stage: {_esc(stage or 'unknown')}",
        f"Reason: {_esc(reason)}",
        "",
        "Nothing was published. The pipeline prefers a missed day to a wrong one.",
    ]))


def crashed(error: str, stage: str = "") -> None:
    _send("\n".join([
        "<b>💥 Pipeline crashed</b>",
        "",
        f"Stage: {_esc(stage or 'unknown')}",
        f"<code>{_esc(error[:800])}</code>",
    ]))
