"""One-time Google Calendar OAuth flow. Run this on your PC, NOT the Uno Q.

The Uno Q is headless, so the interactive browser consent flow can't happen
there. This script runs it locally, then saves a refresh-capable token that
the bot only ever reads and refreshes.

Setup (once):
1. https://console.cloud.google.com/ -> select or create a project.
2. APIs & Services -> Library -> enable "Google Calendar API".
3. APIs & Services -> OAuth consent screen -> User type "External" ->
   fill in the required fields -> under "Test users" add your own Google
   account email (required while the app is unpublished/testing).
4. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
   -> Application type "Desktop app" -> Create -> Download JSON.

Usage:
    python tools/get_google_token.py [path/to/client_secret.json]

Defaults to ./client_secret.json if no path is given. Opens a browser for
consent, then writes ./google_token.json.

Next steps after this script finishes:
1. scp google_token.json to the Uno Q:
       scp google_token.json arduino@<uno-q-host>:/home/arduino/Goob/google_token.json
2. Lock it down on the device:
       chmod 600 /home/arduino/Goob/google_token.json
3. On the device, set CALENDAR_ENABLED=true in .env (and install the
   extra: pip install -e .[calendar]).
"""
from __future__ import annotations

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def main() -> None:
    client_secret_path = Path(sys.argv[1] if len(sys.argv) > 1 else "client_secret.json")
    if not client_secret_path.exists():
        print(f"client secret file not found: {client_secret_path}")
        print("Download it from Google Cloud console (see this script's docstring).")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
    creds = flow.run_local_server(port=0)

    token_path = Path("google_token.json")
    token_path.write_text(creds.to_json(), encoding="utf-8")

    print(f"wrote {token_path.resolve()}")
    print()
    print("Next steps:")
    print("  1. scp google_token.json to the Uno Q:")
    print("       scp google_token.json arduino@<uno-q-host>:/home/arduino/Goob/google_token.json")
    print("  2. On the device: chmod 600 /home/arduino/Goob/google_token.json")
    print("  3. On the device: pip install -e .[calendar]")
    print("  4. Set CALENDAR_ENABLED=true in .env on the device")


if __name__ == "__main__":
    main()
