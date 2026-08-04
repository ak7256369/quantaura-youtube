"""Publish to YouTube via the Data API v3.

Auth is a stored refresh token, minted once locally by get_youtube_token.py.
There is no interactive flow here on purpose — CI cannot answer a browser
consent screen, and a pipeline that needs a human at 13:00 UTC is not
autonomous.

Two policy points are enforced in code rather than left to the operator:
  * `selfDeclaredMadeForKids` is set explicitly (YouTube rejects uploads that
    leave it unset on some channels).
  * The synthetic-content disclosure is written into the description on every
    upload. The API's altered-content flag is set at the channel level in
    Studio, which the README covers; the description line is the part this
    pipeline can guarantee.
"""
from __future__ import annotations

from pathlib import Path

from common import PipelineAbort, config, env, log

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
WATCH_URL = "https://www.youtube.com/watch?v={vid}"


def _service():
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        raise PipelineAbort(
            f"Google API client not installed ({e}). "
            f"Run: pip install -r channel/requirements.txt")

    creds = Credentials(
        token=None,
        refresh_token=env("YT_REFRESH_TOKEN", required=True),
        client_id=env("YT_CLIENT_ID", required=True),
        client_secret=env("YT_CLIENT_SECRET", required=True),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def build_description(script: dict, snapshot: dict, score: dict) -> str:
    cfg = config()
    parts = [script["description"].strip(), ""]

    parts.append(f"Model call for {snapshot['date']}: {snapshot['signal']}"
                 + (f" at {snapshot['confidence']}% confidence."
                    if snapshot.get("confidence") is not None else "."))
    if snapshot.get("gated"):
        parts.append(f"The raw call was {snapshot.get('signal_raw')}, downgraded to HOLD "
                     f"by the confidence gate.")
    parts.append(f"{snapshot['asset_label']} price at publication: {snapshot['price_str']} "
                 f"({snapshot.get('change_24h_pct')}% over 24h).")

    if score.get("resolved_calls"):
        parts += ["", "PUBLIC RECORD",
                  f"{score.get('hits')} correct / {score.get('misses')} wrong "
                  f"over {score.get('resolved_calls')} graded calls "
                  f"({score.get('accuracy_pct')}%).",
                  f"Graded on the realised {score.get('horizon_hours')}-hour price move, "
                  f"with moves inside ±{score.get('flat_band_pct')}% counted as flat."]
    else:
        parts += ["", "PUBLIC RECORD", "This is the first entry. The record starts now."]

    parts += ["", "This video was generated automatically. The narration voice is "
                  "synthetic and the script was written by an AI model from the "
                  "ensemble's real output.",
              cfg["disclaimer"]["description"].strip()]
    return "\n".join(parts)[:4900]


def upload(video: Path, thumb: Path | None, script: dict, snapshot: dict,
           score: dict) -> tuple[str, str]:
    """Returns (video_id, watch_url). Raises PipelineAbort on failure."""
    cfg = config()["upload"]
    service = _service()

    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": script["title"][:95],
            "description": build_description(script, snapshot, score),
            "tags": script["tags"],
            "categoryId": str(cfg["category_id"]),
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": {
            "privacyStatus": cfg["visibility"],
            "selfDeclaredMadeForKids": bool(cfg["made_for_kids"]),
            "license": "youtube",
            "embeddable": True,
        },
    }

    log.info(f"Uploading to YouTube ({cfg['visibility']})...")
    media = MediaFileUpload(str(video), chunksize=4 << 20, resumable=True,
                            mimetype="video/mp4")
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    try:
        while response is None:
            status, response = request.next_chunk()
            if status:
                log.info(f"  {int(status.progress() * 100)}% uploaded")
    except Exception as e:                                       # noqa: BLE001
        raise PipelineAbort(f"YouTube upload failed: {e}")

    vid = response.get("id")
    if not vid:
        raise PipelineAbort(f"Upload returned no video id: {response}")
    url = WATCH_URL.format(vid=vid)
    log.info(f"  Uploaded: {url}")

    if thumb and thumb.exists():
        try:
            service.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb))).execute()
            log.info("  Thumbnail set")
        except Exception as e:                                   # noqa: BLE001
            # Custom thumbnails need a verified channel and are ignored on
            # Shorts anyway — never fail a published video over this.
            log.warning(f"  Thumbnail not set ({e}) — video is live regardless")

    return vid, url
