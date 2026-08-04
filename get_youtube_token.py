"""One-time helper: mint a YouTube refresh token for unattended uploads.

Run this ONCE on your own machine. It opens a browser, you approve the channel,
and it prints a refresh token to paste into the GitHub secret YT_REFRESH_TOKEN.
CI never runs this — a consent screen cannot be answered by a cron job.

    python channel/get_youtube_token.py path/to/client_secret.json

The client_secret.json comes from Google Cloud Console:
    APIs & Services → Credentials → Create credentials → OAuth client ID
    → Application type: Desktop app
"""
from __future__ import annotations

import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


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
        print("Install dependencies first:  pip install -r channel/requirements.txt")
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

    print("\n" + "=" * 66)
    print("Add these three GitHub repository secrets:\n")
    print(f"YT_CLIENT_ID     = {creds.client_id}")
    print(f"YT_CLIENT_SECRET = {creds.client_secret}")
    print(f"YT_REFRESH_TOKEN = {creds.refresh_token}")
    print("=" * 66)
    print("\nDo not commit these. Treat the refresh token like a password.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
