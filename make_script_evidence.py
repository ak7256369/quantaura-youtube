"""Build the reviewer-requested exhibit: the upload script itself.

Google's API-review follow-up (2026-08-06) asked specifically for "a
script/screencast showing how you are uploading videos to YouTube by using
YouTube API services". This renders the genuine source into one PDF:

    p1      what runs where, endpoints, quota — the one-page orientation
    p2-p4   upload.py, complete and verbatim, with line numbers — the file
            that makes every videos.insert call this client performs
    p5      how it is invoked: the pipeline's upload stage and the scheduled
            workflow, so the reviewer sees there is no other upload path
    p6      the companion screencast's shot list, so the two exhibits
            corroborate each other

The source pages are read from disk at build time — this document cannot
drift from the code it testifies about.
"""
from __future__ import annotations

from pathlib import Path

from make_audit_evidence import (CODE_BG, GREEN, INK, M, MUTED, RULE, W,
                                 code_block, font, label, page, para,
                                 wrap_mono)


def header(d, title: str, n: int) -> int:
    """Same layout as the interface exhibit, but this document's own name —
    the reviewer will hold both PDFs and must never confuse them."""
    from make_audit_evidence import ACCENT
    d.text((M, 66), "QuantAura Channel Pipeline", font=font(22, True), fill=ACCENT)
    d.text((W - M, 66), f"Upload Script Evidence · p{n}", font=font(18),
           fill=MUTED, anchor="ra")
    d.line([(M, 104), (W - M, 104)], fill=RULE, width=2)
    d.text((M, 140), title, font=font(30, True), fill=INK)
    return 200

BASE = Path(__file__).resolve().parent
OUT = BASE / "build" / "upload-script-evidence.pdf"

# ~66 numbered source lines fit a page under the header; keep a margin.
LINES_PER_PAGE = 62


def _numbered(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8").splitlines()
    return [f"{i + 1:>3}  {ln}" for i, ln in enumerate(raw)]


def source_pages(path: Path, first_page_no: int, title: str) -> list:
    lines = wrap_mono(_numbered(path), width=100)
    pages = []
    for i in range(0, len(lines), LINES_PER_PAGE):
        chunk = lines[i:i + LINES_PER_PAGE]
        img, d = page()
        n = first_page_no + len(pages)
        cont = " (continued)" if pages else ""
        y = header(d, f"{n} · {title}{cont}", n)
        y = label(d, y, f"{path.name} — verbatim source, "
                        f"lines {i + 1}–{i + len(chunk)}")
        code_block(d, y, chunk, size=13,
                   highlight=("videos().insert", "thumbnails().set",
                              "channels().list", "containsSyntheticMedia",
                              "privacyStatus"))
        pages.append(img)
    return pages


def p_overview():
    img, d = page()
    y = header(d, "1 · How this client uploads to YouTube", 1)
    y = para(d, y,
             "This document responds to the review request for a script showing how "
             "videos are uploaded via YouTube API Services. It contains the complete, "
             "verbatim upload implementation, the code that invokes it, and the "
             "schedule that triggers it. A screencast of the same flow accompanies "
             "this PDF; its shot list is on the final page.")
    y += 10

    y = label(d, y, "The client in one paragraph")
    y = para(d, y,
             "QuantAura Channel Pipeline (Google Cloud project 341500441318) is a "
             "scheduled GitHub Actions job. Once a day it renders a short video "
             "about our own machine-learning model's Bitcoin prediction, then "
             "uploads it to the one channel we own — QuantAura, "
             "youtube.com/@quantaura_ml (UCJFPHehJJdUosFbTabjA8kQ). There is no "
             "user-facing product, no third-party data access, and no upload path "
             "other than the script reproduced in this document.")
    y += 10

    y = label(d, y, "Every YouTube API call the client makes")
    y = code_block(d, y, [
        "videos.insert      1x/day   the upload (1,600 quota units)",
        "                            + 1x/week for a Sunday recap video",
        "thumbnails.set     1x/run   custom thumbnail (50 units)",
        "channels.list      1x/run   mine=true - confirms the upload",
        "                            destination before publishing (1 unit)",
        "",
        "Daily total: ~1,700 of the 10,000-unit default quota.",
    ], highlight=("videos.insert", "thumbnails.set", "channels.list"))

    y = label(d, y, "Where each piece lives")
    y = code_block(d, y, [
        "upload.py        the videos.insert implementation (p2-p4)",
        "pipeline.py      daily orchestrator; calls upload.publish",
        "weekly.py        Sunday recap; calls the same publish()",
        ".github/workflows/       cron triggers: daily 13:00 UTC (+16:00",
        "  daily-video.yml        catch-up), weekly Sunday 15:00 UTC",
        "",
        "Auth: OAuth 2.0 refresh token for our own channel, stored as",
        "GitHub Actions secrets. Scopes: youtube.upload + youtube.readonly.",
    ])

    y = para(d, y,
             "Full source is also available to the review team on request as a "
             "repository export. Contact: khan@quantaura.tech.", size=17,
             fill=MUTED)
    return img


def p_invocation(n: int):
    img, d = page()
    y = header(d, f"{n} · How the script is invoked", n)

    y = label(d, y, "pipeline.py — the only call site of the upload (excerpt)")
    y = code_block(d, y, [
        "# after render + fact-check gates pass:",
        "import upload as upload_mod",
        "_vid, url = upload_mod.upload(video, thumb, script,",
        "                              snapshot, score)",
        "",
        "# upload.upload() builds title/description/tags from the day's",
        "# data and delegates to publish() - reproduced on p2-p4.",
    ], size=15, highlight=("upload_mod.upload",))

    y = label(d, y, ".github/workflows/daily-video.yml — the trigger (excerpt)")
    y = code_block(d, y, [
        "on:",
        "  schedule:",
        "    - cron: '0 13 * * *'   # daily, 13:00 UTC",
        "    - cron: '0 16 * * *'   # catch-up if GitHub drops the first",
        "  workflow_dispatch: {}    # manual runs from the Actions tab",
        "",
        "steps:",
        "  - run: python pipeline.py",
        "    env:",
        "      YT_CLIENT_ID:     ${{ secrets.YT_CLIENT_ID }}",
        "      YT_CLIENT_SECRET: ${{ secrets.YT_CLIENT_SECRET }}",
        "      YT_REFRESH_TOKEN: ${{ secrets.YT_REFRESH_TOKEN }}",
    ], size=15, highlight=("cron:", "pipeline.py"))

    y = para(d, y,
             "The pipeline refuses to publish twice in one UTC day (the catch-up "
             "schedule exists because GitHub's cron is best-effort), uploads are "
             "created private for operator review, and every video carries the "
             "containsSyntheticMedia disclosure plus a not-financial-advice "
             "disclaimer appended in code.")
    return img


def p_screencast(n: int):
    img, d = page()
    y = header(d, f"{n} · The companion screencast", n)
    y = para(d, y,
             "The screencast submitted alongside this document records one complete "
             "production upload, unedited, in three segments:")
    y += 6

    for step, desc in [
        ("1", "GitHub Actions: the 'Daily channel video' workflow is triggered "
              "manually from the Actions tab (workflow_dispatch)."),
        ("2", "The run log, live: fetching the model's prediction, writing and "
              "fact-checking the script, synthesising narration, rendering — then "
              "the upload step printing the destination channel "
              "('Channel: QuantAura (@quantaura_ml)') followed by "
              "'Uploading to YouTube (private)...' and the returned video URL."),
        ("3", "YouTube Studio, immediately after: the same video id visible in "
              "the channel's content list as a new private upload."),
    ]:
        d.ellipse([M + 4, y + 6, M + 26, y + 28], fill=CODE_BG, outline=RULE)
        d.text((M + 15, y + 16), step, font=font(15, True), fill=INK, anchor="mm")
        y = para(d, y, desc, width=88, size=18)
        y += 8

    y += 6
    y = label(d, y, "Why the video in the screencast is private")
    y = para(d, y,
             "All uploads from this client are created with privacyStatus=private "
             "and reviewed by the operator before any visibility change. The "
             "screencast therefore shows the API's actual, everyday behaviour — "
             "not a staged demonstration.")
    return img


def main() -> None:
    pages = [p_overview()]
    pages += source_pages(BASE / "upload.py", 2, "upload.py")
    n = len(pages) + 1
    pages.append(p_invocation(n))
    pages.append(p_screencast(n + 1))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(OUT, "PDF", resolution=150.0, save_all=True,
                  append_images=pages[1:])
    print(f"{OUT}  ({len(pages)} pages, {OUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
