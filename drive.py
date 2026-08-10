"""Mirror the day's video to Google Drive so it can be posted to X by hand.

X moved its API to pay-per-use on 2026-02-06: no free write tier, credits bought
up front, and $0.200 per post that contains a link. The automated X pipeline is
disabled (see the quantaura-x repo) rather than funded. The daily call still has
to reach X, so this module drops the finished mp4 and a ready-to-paste caption
into a Drive folder — posting becomes a download and a paste on a phone.

Scope is `drive.file`, the narrowest Drive scope there is: it can only see and
touch files this app itself created, never the rest of the account's Drive. That
also keeps it clear of the pending YouTube API audit, because Google classifies
drive.file as non-sensitive and it needs no verification review of its own.

Nothing here may fail the day. A Drive outage costs one manual X post, not the
video, so every failure path returns None and lets the pipeline carry on to
YouTube.
"""
from __future__ import annotations

import re
from pathlib import Path

from common import BUILD_DIR, PipelineAbort, config, env, log

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"

# X counts astral-plane codepoints (the signal emoji) as 2 and any URL as a flat
# 23, regardless of the URL's real length.
_URL = re.compile(r"https?://\S+")


def _service():
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        raise PipelineAbort(
            f"Google API client not installed ({e}). "
            f"Run: pip install -r requirements.txt")

    # The Drive grant rides on the same Desktop OAuth client as the YouTube one
    # but keeps its *own* refresh token, so revoking or breaking one consent can
    # never take the channel upload down with it.
    creds = Credentials(
        token=None,
        refresh_token=env("GDRIVE_REFRESH_TOKEN", required=True),
        client_id=env("GDRIVE_CLIENT_ID") or env("YT_CLIENT_ID", required=True),
        client_secret=(env("GDRIVE_CLIENT_SECRET")
                       or env("YT_CLIENT_SECRET", required=True)),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ── Caption ───────────────────────────────────────────────────────────────────

MARK = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}


def weighted_len(text: str) -> int:
    n_urls = len(_URL.findall(text))
    stripped = _URL.sub("", text)
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in stripped) + n_urls * 23


def build_caption(snapshot: dict, score: dict) -> str:
    """The X caption, direction-only.

    Same content policy as the free Telegram channel and the old X pipeline:
    the call and the public record, never the confidence or the per-model votes
    — those are the paid product, and a caption that leaked them would undercut
    the thing it exists to sell. Assembled from the snapshot's numbers only, so
    there is nothing here to fact-check.

    The site link is back in the text: link-bearing posts cost $0.200 each
    through the API, but a post typed by hand costs nothing at all.
    """
    cfg = config()
    site = (cfg.get("links") or {}).get("site", "")
    disclaimer = ((cfg.get("drive") or {}).get("caption_disclaimer")
                  or cfg["disclaimer"]["narration"])
    sig = snapshot["signal"]

    lines = [
        f"{MARK.get(sig, '⚪')} {snapshot['asset_label']} — today's call: {sig}",
        "",
        f"Price {snapshot['price_str']}"
        + (f" ({snapshot['change_24h_pct']:+.2f}% in 24h)"
           if snapshot.get("change_24h_pct") is not None else ""),
    ]
    if score.get("resolved_calls"):
        lines.append(
            f"Record: {score.get('hits', 0)}W/{score.get('misses', 0)}L · "
            f"{score['accuracy_pct']:.0f}% over {score['resolved_calls']} graded calls "
            f"— wins and losses, all public")
    if site:
        lines += ["", f"Confidence + the 4 models' votes: {site}"]
    lines += ["", disclaimer]

    text = "\n".join(lines)
    n = weighted_len(text)
    if n > 280:
        # Drop the record line rather than truncate a number mid-digit. The call
        # and the price are the post; the record is the nice-to-have.
        lines = [ln for ln in lines if not ln.startswith("Record:")]
        text = "\n".join(lines)
        log.warning(f"  Caption was {n} weighted chars — dropped the record line")
        n = weighted_len(text)
        if n > 280:
            # Not truncated: a caption cut mid-number is worse than one the
            # operator has to trim by hand, and X will refuse it loudly anyway.
            log.warning(f"  Caption is still {n} weighted chars — X will reject "
                        f"it as-is; trim it before posting")
    return text


# ── Drive ─────────────────────────────────────────────────────────────────────

def ensure_folder(service) -> str:
    """The folder this app owns, found or created.

    Pinning GDRIVE_FOLDER_ID is optional. Without it the folder is looked up by
    name, which is safe precisely because of the drive.file scope: this listing
    can only ever return folders this app created, so a name match cannot
    collide with something of the user's. Renaming or moving it in the Drive UI
    is fine; renaming it and leaving the config alone makes a fresh one.
    """
    cfg = config().get("drive") or {}
    pinned = env("GDRIVE_FOLDER_ID") or cfg.get("folder_id")
    if pinned:
        return pinned

    name = cfg.get("folder_name") or "QuantAura daily videos"
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    found = service.files().list(
        q=f"mimeType='{FOLDER_MIME}' and name='{safe}' and trashed=false",
        spaces="drive", fields="files(id,name)", pageSize=10,
    ).execute().get("files", [])
    if found:
        return found[0]["id"]

    folder = service.files().create(
        body={"name": name, "mimeType": FOLDER_MIME}, fields="id").execute()
    log.info(f"  Created Drive folder '{name}' ({folder['id']}) — pin it as "
             f"GDRIVE_FOLDER_ID if you plan to rename it")
    return folder["id"]


def check() -> bool:
    """Verify the Drive credentials and destination folder, writing nothing.

    The failure this exists to catch is a CI environment whose
    GDRIVE_REFRESH_TOKEN and YT_CLIENT_ID/SECRET disagree — the token is minted
    locally but spent in Actions, so nothing proves the two match until a real
    run tries it. On a scheduled run that surfaces as a warning *after* the
    video has rendered, hours after the mistake was made; here it surfaces in
    about a minute, on demand.

    Read-only on purpose: auth plus folder resolution is enough to prove the
    pairing, and a check that creates files leaves litter to clean up.
    """
    try:
        service = _service()
        who = service.about().get(fields="user(emailAddress),storageQuota(usage,limit)"
                                  ).execute()
        email = who.get("user", {}).get("emailAddress", "?")
        quota = who.get("storageQuota") or {}
        log.info(f"  Authenticated as {email}")
        if quota.get("limit"):
            used = int(quota.get("usage", 0)) / (1 << 30)
            cap = int(quota["limit"]) / (1 << 30)
            log.info(f"  Drive storage: {used:.1f} / {cap:.0f} GB used")

        folder_id = ensure_folder(service)
        files = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            spaces="drive", orderBy="createdTime desc", pageSize=5,
            fields="files(name,size,createdTime)").execute().get("files", [])

        log.info(f"  Folder: https://drive.google.com/drive/folders/{folder_id}")
        if files:
            log.info(f"  {len(files)} recent file(s):")
            for f in files:
                mb = int(f.get("size") or 0) / (1 << 20)
                log.info(f"    {f['name']}  ({mb:.1f} MB)  {f.get('createdTime', '')[:10]}")
        else:
            log.info("  Folder is empty — nothing has been mirrored yet.")

        log.info("Drive check passed — nothing was written.")
        return True

    except Exception as e:                                       # noqa: BLE001
        log.error(f"Drive check FAILED: {type(e).__name__}: {e}")
        return False


def _upload(service, path: Path, folder_id: str, *, name: str, mime: str,
            description: str = "") -> dict:
    from googleapiclient.http import MediaFileUpload

    body: dict = {"name": name, "parents": [folder_id]}
    if description:
        body["description"] = description[:4000]

    media = MediaFileUpload(str(path), chunksize=4 << 20, resumable=True,
                            mimetype=mime)
    request = service.files().create(body=body, media_body=media,
                                     fields="id,name,size,webViewLink")
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info(f"    {int(status.progress() * 100)}%")
    return response


def mirror(video: Path, snapshot: dict, score: dict) -> dict:
    """Put today's mp4 and caption in Drive.

    Always returns a dict carrying an `ok` flag rather than None on failure:
    callers need to tell "the mirror broke" apart from "the mirror was never
    run" (weekly recaps and --no-upload days), and those two want different
    words in the operator notification.
    """
    caption = ""
    try:
        caption = build_caption(snapshot, score)
        stem = f"quantaura-{snapshot['date']}"

        # Written next to the video whether or not Drive is reachable, so the
        # workflow artifact carries the caption too.
        caption_path = BUILD_DIR / f"{stem}.txt"
        caption_path.write_text(caption + "\n", encoding="utf-8")

        service = _service()
        folder_id = ensure_folder(service)

        log.info("Mirroring to Google Drive...")
        meta = _upload(service, video, folder_id, name=f"{stem}.mp4",
                       mime="video/mp4", description=caption)
        # The caption also goes up as a sibling .txt: the mp4's description is
        # buried in Drive's details pane, and a .txt opens in one tap on a phone.
        _upload(service, caption_path, folder_id, name=f"{stem}.txt",
                mime="text/plain")

        size_mb = round(int(meta.get("size") or 0) / (1 << 20), 1)
        url = meta.get("webViewLink") or ""
        log.info(f"  Drive: {meta.get('name')} ({size_mb} MB) {url}")
        return {"ok": True, "caption": caption, "video_url": url,
                "video_name": meta.get("name"), "size_mb": size_mb}

    except Exception as e:                                       # noqa: BLE001
        # Deliberately broad: a Drive failure must never cost the day's video.
        detail = f"{type(e).__name__}: {e}"
        log.warning(f"  Drive mirror failed ({detail}) — the mp4 is still in "
                    f"the run artifact")
        return {"ok": False, "error": detail, "caption": caption}
