"""Google Calendar read access (config.CALENDAR_ENABLED).

Plain client-side calls to Google's API, not an MCP server — see
documentation/OPTIMIZATION.md's backlog note. OAuth happens once on the
user's PC via tools/get_google_token.py; this module only refreshes the
saved token, it never runs the interactive consent flow.

All googleapiclient/google-auth imports are deferred into functions so the
module (and the rest of the bot) imports fine when the `calendar` extra
isn't installed and CALENDAR_ENABLED is false.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

from src import config

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

_REPO_ROOT = Path(__file__).resolve().parent.parent


class CalendarError(RuntimeError):
    pass


def _token_path() -> Path:
    path = Path(config.GOOGLE_TOKEN_PATH)
    return path if path.is_absolute() else _REPO_ROOT / path


_service = None


def _get_service():
    global _service
    if _service is not None:
        return _service

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_path = _token_path()
    if not token_path.exists():
        raise CalendarError(
            f"no Google token at {token_path} — run tools/get_google_token.py "
            "on your PC and scp the result here"
        )

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception as exc:
        raise CalendarError(
            f"could not parse the Google token at {token_path} ({exc}) — "
            "re-run tools/get_google_token.py on your PC and scp the new token"
        ) from exc
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise CalendarError(
                f"Google token refresh failed ({exc}) — it may be revoked or "
                "expired, re-run tools/get_google_token.py and scp the new token"
            ) from exc
        # Atomic rewrite: an interrupted in-place write would corrupt the
        # only credential and force the full PC-side OAuth flow again.
        # Create the temp with owner-only mode so the replace preserves
        # the chmod 600 the setup instructions require.
        tmp = token_path.with_suffix(".json.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(creds.to_json())
            os.replace(tmp, token_path)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    _service = build("calendar", "v3", credentials=creds)
    return _service


def _human_date(value: str) -> str:
    return date.fromisoformat(value).strftime("%a %b %d %Y")


def _human_datetime(value: str) -> str:
    return datetime.fromisoformat(value).astimezone().strftime("%a %b %d %-I:%M %p")


def _human_time(value: str) -> str:
    return datetime.fromisoformat(value).astimezone().strftime("%-I:%M %p")


def _format_start_end(item: dict) -> tuple[str, str, bool]:
    # Events carry either "date" (all-day) or "dateTime" (timed). end is
    # time-only for timed events — same-day meetings are the common case.
    start_raw = item.get("start", {})
    end_raw = item.get("end", {})
    if "date" in start_raw:
        return _human_date(start_raw["date"]), _human_date(end_raw.get("date", start_raw["date"])), True
    start_dt = start_raw.get("dateTime", "")
    end_dt = end_raw.get("dateTime", "")
    return _human_datetime(start_dt) if start_dt else "", _human_time(end_dt) if end_dt else "", False


def get_events(time_min_iso: str, time_max_iso: str, max_results: int = 10) -> list[dict]:
    service = _get_service()
    try:
        response = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min_iso,
                timeMax=time_max_iso,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except Exception as exc:
        raise CalendarError(f"Google Calendar request failed: {exc}") from exc

    events = []
    for item in response.get("items", []):
        start, end, all_day = _format_start_end(item)
        events.append({
            "summary": item.get("summary", "(no title)"),
            "start": start,
            "end": end,
            "location": item.get("location", ""),
            "all_day": all_day,
        })
    return events
