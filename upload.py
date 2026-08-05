"""Publish to YouTube via the Data API v3.

Auth is a stored refresh token, minted once locally by get_youtube_token.py.
There is no interactive flow here on purpose — CI cannot answer a browser
consent screen, and a pipeline that needs a human at 13:00 UTC is not
autonomous.

Two policy points are enforced in code rather than left to the operator:
  * `selfDeclaredMadeForKids` is set explicitly (YouTube rejects uploads that
    leave it unset on some channels).
  * `containsSyntheticMedia` carries the altered/synthetic-content disclosure,
    so it is never a per-video click in Studio. Note the field means
    *realistic* synthetic content — a real person appearing to say something
    they did not, altered footage of a real event, a realistic scene that never
    happened. A drawn chart narrated by a generic synthetic voice does not meet
    that bar, so `upload.contains_synthetic_media` is a judgement call rather
    than an obligation; it ships as true because a channel whose whole premise
    is disclosure should over-disclose rather than under-disclose.
"""
from __future__ import annotations

from pathlib import Path

from common import PipelineAbort, config, env, log

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
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


def publish(video: Path, thumb: Path | None, *, title: str, description: str,
            tags: list[str]) -> tuple[str, str]:
    """Upload one video with the channel's standard status flags.

    The single place a video leaves this pipeline: the daily upload and any
    one-off (intro videos, weekly recaps) all pass through here, so the
    disclosure flags, kids declaration, and destination-channel logging can
    never diverge between video types.

    Returns (video_id, watch_url). Raises PipelineAbort on failure.
    """
    cfg = config()["upload"]
    service = _service()

    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": title[:95],
            "description": description[:4900],
            "tags": tags,
            "categoryId": str(cfg["category_id"]),
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": {
            "privacyStatus": cfg["visibility"],
            "selfDeclaredMadeForKids": bool(cfg["made_for_kids"]),
            "containsSyntheticMedia": bool(cfg["contains_synthetic_media"]),
            "license": "youtube",
            "embeddable": True,
        },
    }

    # Log the destination on every run. If a token is ever re-minted against
    # the wrong channel, this line is where it shows up — before the video
    # does, and in a log that is kept.
    try:
        items = service.channels().list(part="snippet", mine=True).execute().get("items", [])
        if items:
            log.info(f"  Channel: {items[0]['snippet'].get('title')} "
                     f"({items[0]['snippet'].get('customUrl') or items[0].get('id')})")
    except Exception as e:                                       # noqa: BLE001
        log.warning(f"  Could not read the bound channel ({e}) — continuing")

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


def upload(video: Path, thumb: Path | None, script: dict, snapshot: dict,
           score: dict) -> tuple[str, str]:
    """Daily-video entry point: builds the metadata from the day's script and
    snapshot, then hands off to publish()."""
    return publish(video, thumb,
                   title=script["title"],
                   description=build_description(script, snapshot, score),
                   tags=script["tags"])
