"""One-time helper: mint a Google Drive refresh token for unattended uploads.

Run this ONCE on your own machine. It opens a browser, you approve the Google
account, and it prints a refresh token to paste into the GitHub secret
GDRIVE_REFRESH_TOKEN. CI never runs it — a consent screen cannot be answered by
a cron job.

    python get_drive_token.py path/to/client_secret.json

Reuse the same Desktop OAuth client json as get_youtube_token.py: one Google
Cloud project, two separate grants. The token minted here carries *only*
drive.file, so it cannot touch the YouTube channel, and the YouTube token cannot
touch Drive.

Add the Drive API to the project first if you have not:
    APIs & Services → Library → Google Drive API → Enable

drive.file is a non-sensitive scope, so this needs no verification review and
cannot affect the pending YouTube API audit. On an unverified app you will still
see the "Google hasn't verified this app" interstitial — click Advanced →
"Go to <app> (unsafe)". That warning is about *other people* trusting your app;
you are consenting to your own.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The narrowest Drive scope: per-file access to files this app creates, and no
# visibility at all into anything else in the account.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

FOLDER_MIME = "application/vnd.google-apps.folder"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    secret = Path(sys.argv[1])
    if not secret.exists():
        print(f"No such file: {secret}")
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Install dependencies first:  pip install -r requirements.txt")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    # access_type=offline + prompt=consent is what actually produces a refresh
    # token; without prompt=consent Google reuses a prior grant and returns
    # none, which is the usual reason this step "silently fails".
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print("\nNo refresh token returned. Revoke the app's access at "
              "https://myaccount.google.com/permissions and run this again.")
        return 1

    # Create the destination folder now, so the first CI run does not have to,
    # and so the id can be pinned before anything is written to the account.
    folder_id = None
    try:
        from googleapiclient.discovery import build
        import drive as drive_mod

        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        folder_id = drive_mod.ensure_folder(service)
        about = service.about().get(fields="user(emailAddress)").execute()
        print("\n" + "=" * 66)
        print(f"VIDEOS WILL GO TO:  {about.get('user', {}).get('emailAddress')}")
        print(f"  folder id:  {folder_id}")
        print("  open it:    https://drive.google.com/drive/folders/" + folder_id)
    except Exception as e:                                       # noqa: BLE001
        print(f"\nCould not create or confirm the Drive folder: {e}")
        print("Not fatal — the first pipeline run will create it.")

    print("\n" + "=" * 66)
    print("Add these GitHub repository secrets:\n")
    print(f"GDRIVE_REFRESH_TOKEN = {creds.refresh_token}")
    if folder_id:
        print(f"GDRIVE_FOLDER_ID     = {folder_id}   (optional — pins the folder"
              " so renaming it in Drive is safe)")
    print("\nGDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET are only needed if this is")
    print("a different OAuth client from the YouTube one; otherwise the")
    print("pipeline falls back to YT_CLIENT_ID / YT_CLIENT_SECRET.")
    print("=" * 66)
    print("\nDo not commit these. Treat the refresh token like a password.")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
