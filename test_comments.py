"""Checks for comments.py — no network, no state written.

    python test_comments.py

The filter is the only thing standing between a stranger's comment and a
published, uneditable video, so the cases worth pinning are the rejections.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import comments
import notify

passed = 0


def ok(cond: bool, label: str) -> None:
    global passed
    assert cond, label
    passed += 1
    print(f"  ✓ {label}")


def c(i: int, text: str, *, likes: int = 0, own: bool = False, hours_ago: float = 1) -> dict:
    now = datetime.now(timezone.utc)
    return {"id": f"c{i}", "video_id": "v", "video_url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
            "video_date": "2026-11-16", "video_kind": "daily", "author": f"A{i}", "own": own,
            "text": text, "likes": likes, "replies": 0,
            "published_at": (now - timedelta(hours=hours_ago)).isoformat(),
            "url": f"https://www.youtube.com/watch?v=aaaaaaaaaaa&lc=c{i}"}


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    comments.SEEN_PATH = tmp / "comments_seen.json"       # never touch real state

    print("── clean_question ──")
    good = "Why does the model say hold so often?"
    ok(comments.clean_question(f"  {good}\n") == good, "a plain question passes, whitespace collapsed")
    for label, text in [
        ("not a question", "The model says hold a lot."),
        ("too short", "Why hold?"),
        ("too long", "Why does the model " + "really " * 30 + "hold?"),
        ("a link", "Why does the model hold? see www.example.com"),
        ("a bare domain", "Why does the model hold, check signals.io ok?"),
        ("an @mention", "@someone why does the model hold so often?"),
        ("a hashtag", "Why does the model hold so often? #bitcoin"),
        ("digits", "Is the accuracy really 90 percent on hold calls?"),
        ("non-ASCII / homoglyphs", "Why does the modеl say hold so often?"),
        ("shouting", "WHY DOES THE MODEL SAY HOLD SO OFTEN?"),
        ("a solicitation", "Why hold when you can message me on WhatsApp instead?"),
        ("a banned phrase", "Is the hold call guaranteed to be right this week?"),
        ("abuse", "Why is this stupid model always saying hold?"),
        ("empty", ""),
    ]:
        ok(comments.clean_question(text) is None, f"rejected: {label}")

    print("── topic_for ──")
    ok(comments.topic_for(good)[0] == "hold", "hold question maps to the gate answer")
    ok(comments.topic_for("Is the model actually profitable after fees?")[0] == "portfolio", "profit question maps to the portfolio answer")
    ok(comments.topic_for("How do you decide if a call was correct?") is None
       and comments.topic_for("Do you only cover bitcoin or other coins too?") is None,
       "grading and coverage questions are left to the fixed method and watchlist scenes")
    ok(comments.topic_for("What is your favourite colour and why?") is None, "off-topic question has no answer")
    ok(comments.topic_for("Is the threshold too high?")[0] == "hold" and comments.topic_for("Why withhold it?") is None,
       "keywords match whole words only")
    for _id, _kw, heading, answer in comments.FAQ:
        text = " ".join(answer)
        assert not any(ch.isdigit() for ch in text), f"FAQ answer '{heading}' contains a digit"
        assert 2 <= len(answer) <= 4
    ok(True, "every prepared answer is 2-4 sentences with no figures")

    print("── pick_question ──")
    pool = [
        c(1, good, likes=2),
        c(2, "Is the model actually profitable after fees?", likes=9),
        c(3, "Why do you show the losses and not hide them?", likes=50, own=True),
        c(4, "Should I buy now or will it reach the moon?", likes=99),
        c(5, "What model is this, an ensemble?", likes=30, hours_ago=24 * 20),
        c(6, "Great video, thanks", likes=70),
    ]
    q = comments.pick_question(pool)
    ok(q and q["comment_id"] == "c2", "most-liked eligible viewer question wins")
    ok(set(q) == {"comment_id", "question", "topic", "heading", "answer"}, "the pick carries no author name")
    ok(comments.pick_question(pool, avoid_topics=("portfolio",))["comment_id"] == "c1",
       "a topic the research segment covers this week is skipped")
    comments.mark_answered("c2")
    ok(comments.pick_question(pool)["comment_id"] == "c1", "an answered question is not reused")
    ok(json.loads(comments.SEEN_PATH.read_text())["answered"] == ["c2"], "state records the id only")
    ok(comments.pick_question([]) is None and comments.pick_question(None) is None, "no comments, no question")
    ok(comments.pick_question([pool[3], pool[5]]) is None, "nothing eligible means no segment")

    print("── run log → videos ──")
    runs = tmp / "runs.jsonl"
    today = datetime.now(timezone.utc)
    d = lambda n: (today - timedelta(days=n)).strftime("%Y-%m-%d")   # noqa: E731
    runs.write_text("\n".join(json.dumps(r) for r in [
        {"date": d(1), "kind": "daily", "status": "published", "url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"},
        {"date": d(2), "kind": "daily", "status": "rendered", "detail": "upload failed"},
        {"date": d(3), "kind": "weekly", "status": "published", "url": "https://www.youtube.com/watch?v=BBBBBBBBBBB"},
        {"date": d(40), "kind": "daily", "status": "published", "url": "https://www.youtube.com/watch?v=CCCCCCCCCCC"},
        {"date": d(1), "kind": "daily", "status": "published", "url": None},
    ]) + "\nnot json\n")
    comments.RUNS_PATH = runs
    vids = comments.recent_videos(14)
    ok([v["id"] for v in vids] == ["AAAAAAAAAAA", "BBBBBBBBBBB"], "published videos inside the window, newest first")

    print("── API parsing ──")
    item = {"id": "T1", "snippet": {"channelId": "UCchan", "totalReplyCount": 2, "topLevelComment": {
        "id": "C1", "snippet": {"authorDisplayName": "<b>Eve</b>", "authorChannelId": {"value": "UCeve"},
                                "textOriginal": "Nice <script>", "likeCount": 3, "publishedAt": "2026-11-16T10:00:00Z"}}}}
    parsed = comments._parse_thread(item, vids[0])
    ok(parsed["id"] == "C1" and parsed["likes"] == 3 and parsed["replies"] == 2 and parsed["own"] is False, "thread parsed")
    item["snippet"]["topLevelComment"]["snippet"]["authorChannelId"]["value"] = "UCchan"
    ok(comments._parse_thread(item, vids[0])["own"] is True, "the channel's own comment is flagged")
    ok(comments._parse_thread({"id": "x"}, vids[0]) is None, "a malformed item is skipped, not fatal")

    print("── digest + seen state ──")
    body = notify.comments_digest([parsed], dry_run=True)
    ok("&lt;b&gt;Eve&lt;/b&gt;" in body and "&lt;script&gt;" in body, "author and text are HTML-escaped")
    many = [c(100 + i, "x" * 600) for i in range(40)]
    body = notify.comments_digest(many, dry_run=True)
    ok(len(body) < 4000 and "more in YouTube Studio" in body, "a long digest is trimmed by whole comments, under Telegram's limit")
    ok(body.count("<b>") == body.count("</b>") and body.count("<a ") == body.count("</a>"), "trimmed digest keeps its tags balanced")
    state = comments._load_state()
    fresh = comments.new_comments(pool, state)
    ok([x["id"] for x in fresh].count("c3") == 0 and len(fresh) == 5, "new comments exclude the channel's own")
    state["seen"]["c1"] = d(0)
    state["seen"]["old"] = d(400)
    comments._save_state(state, d(0))
    saved = json.loads(comments.SEEN_PATH.read_text())
    ok("old" not in saved["seen"] and "c1" in saved["seen"], "seen ids are pruned after the TTL")
    ok(good not in comments.SEEN_PATH.read_text(), "no comment text is ever written to state")

    print(f"\n✅ {passed} comment checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
