"""Read what viewers wrote under the channel's videos.

    python comments.py              # fetch, send the operator digest, save state
    python comments.py --dry-run    # synthetic comments, no network, no state
    python comments.py --no-notify  # fetch and save state, send nothing

Two consumers:

  1. The operator digest. New top-level comments go to the operator's Telegram
     chat, so replying does not depend on remembering to open YouTube Studio.
  2. The weekly recap's "from the comments" segment (see weekly.py), which
     answers one viewer question a week.

How it is built, and why:

  * Video ids come from this repo's own run log. Every published row in
    state/runs.jsonl carries the watch URL, so there is no channel lookup and no
    uploads-playlist walk.
  * Comments are read with an API key (YT_API_KEY), not the upload OAuth token.
    Public comments are public data: an API key reads them, and using one keeps
    the audited upload consent at exactly the scopes it was approved with.
  * Only comment IDS are written to state. This repository is public; the text
    of a comment and the name of its author belong on YouTube, where the author
    can still edit or delete them, and are never committed here.
  * Nothing here can fail the day. A missing key, a quota error or comments
    being disabled on a video is a log line. Reading comments is a courtesy,
    publishing the call is the job.

Viewer text is untrusted input. Nothing a commenter writes is ever sent to a
language model, and nothing they write is ever spoken: the weekly segment shows
one filtered question on screen and answers it with prose written in advance
(FAQ below). The filter's job is only to decide whether a comment is fit to be
SHOWN, which is a much smaller thing to get right than deciding what to say.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import STATE_DIR, RUNS_PATH, config, env, get_json, log, read_json, write_json

API = "https://www.googleapis.com/youtube/v3/commentThreads"
SEEN_PATH = STATE_DIR / "comments_seen.json"
_VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")

# How long a seen id is remembered. Comfortably longer than the video lookback,
# so a comment cannot fall out of memory while its video is still being read.
SEEN_TTL_DAYS = 120


def _cfg() -> dict:
    return config().get("comments") or {}


# ── Which videos ──────────────────────────────────────────────────────────────

def recent_videos(days: int | None = None, now: datetime | None = None) -> list[dict]:
    """Videos this pipeline published in the last `days`, newest first."""
    days = int(days if days is not None else _cfg().get("lookback_days", 14))
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    if not RUNS_PATH.exists():
        return []
    videos: dict[str, dict] = {}
    for line in RUNS_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except Exception:                                        # noqa: BLE001
            continue
        if row.get("status") != "published" or str(row.get("date", "")) < cutoff:
            continue
        m = _VIDEO_ID_RE.search(str(row.get("url") or ""))
        if not m:
            continue
        videos[m.group(1)] = {
            "id": m.group(1),
            "url": row["url"],
            "date": row.get("date"),
            "kind": row.get("kind", "daily"),
            "title": row.get("detail") or "",
        }
    return sorted(videos.values(), key=lambda v: v["date"] or "", reverse=True)


# ── Fetch ─────────────────────────────────────────────────────────────────────

def _parse_thread(item: dict, video: dict) -> dict | None:
    try:
        thread = item["snippet"]
        top = thread["topLevelComment"]
        s = top["snippet"]
    except (KeyError, TypeError):
        return None
    author_channel = (s.get("authorChannelId") or {}).get("value")
    return {
        "id": top.get("id") or item.get("id"),
        "video_id": video["id"],
        "video_url": video["url"],
        "video_date": video.get("date"),
        "video_kind": video.get("kind"),
        "author": s.get("authorDisplayName") or "",
        # A comment by the channel itself (a pinned note, a reply-to-all) is not
        # a viewer and must never be offered back as a "viewer question".
        "own": bool(author_channel and author_channel == thread.get("channelId")),
        "text": s.get("textOriginal") or s.get("textDisplay") or "",
        "likes": int(s.get("likeCount") or 0),
        "replies": int(thread.get("totalReplyCount") or 0),
        "published_at": s.get("publishedAt") or "",
        "url": f"https://www.youtube.com/watch?v={video['id']}&lc={top.get('id') or item.get('id')}",
    }


def fetch_video(video: dict, key: str, limit: int) -> list[dict]:
    """Newest top-level comments on one video. One page: `limit` is capped at
    the API's page size, and a daily Short does not draw more than that between
    two runs."""
    res = get_json(API, params={
        "part": "snippet", "videoId": video["id"], "order": "time",
        "maxResults": max(1, min(int(limit), 100)), "textFormat": "plainText",
        "key": key,
    }, retries=2, label=f"comments {video['id']}")
    if not res.ok:
        err = (res.error or "").replace(key, "***")       # never log the key
        if "commentsDisabled" in err:
            log.info(f"  {video['id']}: comments are disabled")
        elif res.status == 404 or "videoNotFound" in err:
            log.info(f"  {video['id']}: video not found (private or deleted)")
        else:
            log.warning(f"  {video['id']}: comments unavailable — {err[:160]}")
        return []
    out = []
    for item in (res.data or {}).get("items") or []:
        c = _parse_thread(item, video)
        if c and c["id"]:
            out.append(c)
    return out


def fetch_all(days: int | None = None) -> list[dict] | None:
    """Every recent comment, or None when comment reading is not configured.

    None and [] are different answers: None means "could not look", [] means
    "looked, nobody wrote anything". Callers that only want comments treat both
    as empty; the CLI reports them differently.
    """
    cfg = _cfg()
    if not cfg.get("enabled", True):
        log.info("Comment reading is disabled in config.yaml.")
        return None
    key = env("YT_API_KEY")
    if not key:
        log.info("YT_API_KEY is not set — comment reading skipped.")
        return None
    videos = recent_videos(days)
    if not videos:
        log.info("No published videos in the lookback window — no comments to read.")
        return []
    log.info(f"Reading comments on {len(videos)} video(s)...")
    comments: list[dict] = []
    for v in videos:
        comments += fetch_video(v, key, int(cfg.get("max_per_video", 100)))
    return comments


# ── State: ids only ───────────────────────────────────────────────────────────

def _load_state() -> dict:
    state = read_json(SEEN_PATH, {}) or {}
    state.setdefault("seen", {})
    state.setdefault("answered", [])
    return state


def _save_state(state: dict, today: str) -> None:
    cutoff = (datetime.strptime(today, "%Y-%m-%d")
              - timedelta(days=SEEN_TTL_DAYS)).strftime("%Y-%m-%d")
    state["seen"] = {k: v for k, v in sorted(state["seen"].items()) if v >= cutoff}
    state["answered"] = sorted(set(state["answered"]))[-200:]
    write_json(SEEN_PATH, state)


def mark_answered(comment_id: str) -> None:
    """Remember that the weekly recap has used this question, so next week's
    recap does not put the same comment on screen again."""
    if not comment_id:
        return
    state = _load_state()
    state["answered"].append(comment_id)
    _save_state(state, datetime.now(timezone.utc).strftime("%Y-%m-%d"))


# ── The weekly question ───────────────────────────────────────────────────────
# Each entry: (id, keywords, on-screen heading, spoken answer).
#
# The answers are written here, by hand, and are true whatever a commenter
# asks — the same reason weekly.py keeps its method and research segments as
# fixed prose. They restate things the channel already says elsewhere and
# contain no figures, so there is nothing in them for a week's data to
# contradict. A question is only eligible if it lands on one of these topics.
#
# Two obvious topics are absent on purpose: how a call is graded, and which
# other coins are covered. The recap's fixed `method` and `watchlist` scenes
# answer both every single week, so a segment on either would be the same
# explanation twice within a minute.

FAQ: list[tuple[str, tuple[str, ...], str, list[str]]] = [
    # First on purpose: "should I buy?" is the most common question a market
    # channel gets and the one that most needs a careful, fixed answer.
    ("advice",
     ("should i buy", "should i sell", "should i invest", "should we buy", "should we sell",
      "buy or not", "sell or not", "buy or sell", "good time to buy", "time to buy",
      "worth buying", "is it safe to buy", "should i hold"),
     "Should I buy today?",
     ["Nobody on this channel can tell you whether to buy, and the model cannot either.",
      "Each call is a research output from an automated model, published so its "
      "record can be checked, and it is often wrong.",
      "Treat it as one data point, not as a reason to trade, and never risk money "
      "you cannot afford to lose."]),
    ("hold",
     ("hold", "confidence", "gate", "threshold", "no signal", "never buy", "never sell"),
     "Why so many holds?",
     ["The model produces a direction every day, but a confidence gate sits in front of it.",
      "When conviction is below the threshold, the published call becomes hold. "
      "A forced guess would look decisive and grade like a coin flip.",
      "So hold is the most common call here, and it is meant to be."]),
    ("models",
     ("which model", "what model", "lstm", "transformer", "xgboost", "kan", "ensemble",
      "neural", "architecture", "how does the model", "how does it work", "algorithm"),
     "What is the model?",
     ["The call comes from an ensemble of four different model families, blended "
      "with learned weights.",
      "They read the market differently, so they fail differently, and one model's "
      "blind spot can be another model's signal.",
      "None of the four is treated as the best one."]),
    ("accuracy",
     ("accuracy", "accurate", "wrong", "coin flip", "trust", "losses", "why publish",
      "why show", "reliable", "is this real", "legit"),
     "Why show the misses?",
     ["The model is often wrong, and that is the reason the scoreboard is public.",
      "A record that only showed the wins would tell you nothing about the model "
      "and a lot about the editing.",
      "Wins and losses stay on the board, graded by the same rule."]),
    ("portfolio",
     ("profit", "profitable", "portfolio", "fees", "buy and hold", "beat the market",
      "make money", "real money", "trade it", "backtest"),
     "Does it make money?",
     ["The paper portfolio is virtual money traded mechanically on the same "
      "committed calls, with fees included, next to simply holding.",
      "A model can classify well and still not be worth trading after costs, "
      "which is why both numbers are shown together.",
      "No real money is traded on these calls."]),
    ("training",
     ("retrain", "retrained", "training", "trained", "learn", "learning", "update the model",
      "updated", "what data", "dataset"),
     "Does the model keep learning?",
     ["The models are fine tuned on fresh market data on a regular schedule.",
      "Older training data stays in the mix, so adapting to this month does not "
      "erase what earlier history taught them.",
      "An update that scores worse than the current model is rejected."]),
]

_URL_RE = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|net|org|io|me|xyz|ly|gg|co)\b|t\.me", re.I)
# Not a profanity filter with ambitions: a short list of the words that mark a
# comment as spam, a solicitation, or simply not something to put on screen.
_BLOCKLIST = ("whatsapp", "telegram", "dm me", "inbox me", "contact me", "invest with",
              "account manager", "recovery", "giveaway", "airdrop", "signal group",
              "scam", "fuck", "shit", "idiot", "stupid", "bitch", "retard")


def _matches(text: str, keyword: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(keyword)}(?![\w-])", text) is not None


def topic_for(text: str) -> tuple[str, str, list[str]] | None:
    """The FAQ topic a question is about, or None. First match wins, and the
    FAQ is ordered most-specific first for that reason."""
    lowered = text.lower()
    for topic_id, keywords, heading, answer in FAQ:
        if any(_matches(lowered, k) for k in keywords):
            return topic_id, heading, answer
    return None


def clean_question(text: str) -> str | None:
    """The comment as it may appear on screen, or None if it may not.

    Deliberately strict. Rejecting a perfectly good question costs one optional
    segment in one video; showing a bad one puts a stranger's words in a
    published video that cannot be edited afterwards.
    """
    cfg = _cfg()
    q = " ".join((text or "").split())
    lo, hi = int(cfg.get("question_min_chars", 15)), int(cfg.get("question_max_chars", 160))
    if not lo <= len(q) <= hi or "?" not in q:
        return None
    if _URL_RE.search(q) or "@" in q or "#" in q:
        return None
    # No digits at all: a number inside the question ("is it really ninety
    # percent?") would sit on screen next to the real record and read as a
    # statistic the channel is endorsing.
    if any(ch.isdigit() for ch in q):
        return None
    # English, plainly typed. Non-ASCII is where homoglyph spam and text the
    # caption font cannot draw both live.
    if any(ord(ch) > 126 or ord(ch) < 32 for ch in q):
        return None
    letters = [ch for ch in q if ch.isalpha()]
    if len(letters) < 10 or sum(ch.isupper() for ch in letters) > 0.5 * len(letters):
        return None
    lowered = q.lower()
    if any(w in lowered for w in _BLOCKLIST):
        return None
    for phrase in config()["factcheck"]["banned_phrases"]:
        if _matches(lowered, phrase.lower()):
            return None
    return q


def pick_question(comments: list[dict] | None, *, days: int = 8,
                  avoid_topics: tuple[str, ...] = (),
                  now: datetime | None = None,
                  setting: str = "use_in_weekly",
                  include_own: bool = False) -> dict | None:
    """The one viewer question this week's recap answers, or None.

    Most-liked eligible question wins; ties go to the earliest. `avoid_topics`
    (FAQ ids) lets the caller skip a topic its rotating research segment
    already covers this week, so the recap does not explain the same thing
    twice, two scenes apart.
    """
    # include_own exists for a manual demo run only: the channel's own comment
    # is never a "viewer" in normal operation.
    if not comments or not _cfg().get(setting, True):
        return None
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()
    answered = set(_load_state()["answered"])
    candidates = []
    for c in comments:
        if (c.get("own") and not include_own) or c["id"] in answered or (c.get("published_at") or "") < cutoff:
            continue
        q = clean_question(c.get("text", ""))
        topic = topic_for(q) if q else None
        if not topic or topic[0] in avoid_topics:
            continue
        candidates.append((-c.get("likes", 0), c.get("published_at") or "", {
            "comment_id": c["id"], "question": q, "topic": topic[0],
            "heading": topic[1], "answer": topic[2],
        }))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1]))
    return candidates[0][2]


# ── Digest ────────────────────────────────────────────────────────────────────

def new_comments(comments: list[dict], state: dict) -> list[dict]:
    fresh = [c for c in comments if c["id"] not in state["seen"] and not c.get("own")]
    return sorted(fresh, key=lambda c: c.get("published_at") or "")


def _dry_comments() -> list[dict]:
    now = datetime.now(timezone.utc)
    vid = {"id": "dryrun00000", "url": "https://www.youtube.com/watch?v=dryrun00000",
           "date": now.strftime("%Y-%m-%d"), "kind": "daily"}

    def mk(i, author, text, likes=0, own=False):
        return {"id": f"dry{i}", "video_id": vid["id"], "video_url": vid["url"],
                "video_date": vid["date"], "video_kind": "daily", "author": author,
                "own": own, "text": text, "likes": likes, "replies": 0,
                "published_at": (now - timedelta(hours=i)).isoformat(),
                "url": f"{vid['url']}&lc=dry{i}"}

    return [
        mk(1, "Viewer One", "Why does the model say hold so often?", likes=4),
        mk(2, "Viewer Two", "Nice, clear and honest. Subscribed."),
        mk(3, "Spammer", "Message me on WhatsApp for guaranteed 10x signals!!! www.example.com"),
        mk(4, "QuantAura Signals", "Thanks for watching.", own=True),
        mk(5, "Viewer Three", "Is the model actually profitable after fees?", likes=1),
    ]


def run(dry_run: bool = False, send: bool = True) -> int:
    import notify

    if dry_run:
        comments = _dry_comments()
        fresh = [c for c in comments if not c.get("own")]
        log.info(f"DRY RUN — {len(comments)} synthetic comments, {len(fresh)} from viewers")
        print(notify.comments_digest(fresh, dry_run=True))
        q = pick_question(comments)
        log.info(f"Weekly question: {q['question']!r} → {q['heading']}" if q
                 else "Weekly question: none eligible")
        return 0

    comments = fetch_all()
    if comments is None:
        return 0
    state = _load_state()
    fresh = new_comments(comments, state)
    log.info(f"  {len(comments)} comment(s) read, {len(fresh)} new")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if fresh and send:
        delivered = notify.comments_digest(fresh)
        if not delivered:
            # Leave them unseen: tomorrow's run tries again rather than
            # swallowing comments the operator was never shown.
            log.warning("  Digest not delivered — comments stay unread for the next run.")
            return 0
    for c in comments:
        state["seen"].setdefault(c["id"], today)
    _save_state(state, today)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="synthetic comments, no network, no state written")
    ap.add_argument("--no-notify", action="store_true",
                    help="read and remember comments without sending the digest")
    args = ap.parse_args()
    try:
        return run(dry_run=args.dry_run, send=not args.no_notify)
    except Exception as e:                                       # noqa: BLE001
        # The contract in the module docstring: this never fails the day.
        log.warning(f"Comment reading failed ({type(e).__name__}: {e}) — continuing.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
