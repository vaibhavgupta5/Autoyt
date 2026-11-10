"""YouTube Data API: login, resumable upload, thumbnail."""
from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path

from .config import Settings
from .errors import ActionNeeded, UploadLimit

log = logging.getLogger("autoyt.upload")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
RETRIABLE_STATUS = {500, 502, 503, 504}
CATEGORY = {"funny": "23", "informative": "27", "deep": "22"}  # Comedy, Education, People & Blogs


def run_auth(s: Settings, bind: str = "0.0.0.0", port: int = 8765) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = s.secrets_dir / "client_secret.json"
    if not secret.exists():
        raise ActionNeeded(f"{secret} not found. Download the OAuth Desktop client JSON from Google Cloud.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(
        host="localhost", bind_addr=bind, port=port, open_browser=False,
        access_type="offline", prompt="consent",
        authorization_prompt_message="Open this URL in your browser and approve access:\n{url}\n",
        success_message="AutoYT is authorized. You can close this tab.",
    )
    token = s.secrets_dir / "token.json"
    token.write_text(creds.to_json())
    token.chmod(0o600)
    print(f"Saved {token}")


def get_service(s: Settings):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token = s.secrets_dir / "token.json"
    if not token.exists():
        raise ActionNeeded("YouTube login missing. Run: docker compose run --rm -p 8765:8765 autoyt auth")
    creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise ActionNeeded("YouTube login has no refresh token. Run `autoyt auth` again.")
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise ActionNeeded(f"YouTube login expired or was revoked ({e}). Run `autoyt auth` again. "
                               "If it keeps expiring, set the OAuth app to 'In production'.") from e
        token.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _reason(err) -> str:
    try:
        return json.loads(err.content.decode())["error"]["errors"][0]["reason"]
    except Exception:  # noqa: BLE001
        return ""


def upload_video(service, path: Path, *, title: str, description: str, tags: list[str], category: str,
                 publish_at: str | None, privacy: str = "private") -> str:
    """Resumable upload. Returns the video id. Raises UploadLimit / ActionNeeded where relevant.

    With publish_at the video is held (YouTube requires private) and goes PUBLIC at that time.
    Without it, `privacy` decides: "public" publishes immediately.
    """
    import httplib2
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    status: dict = {"privacyStatus": "private" if publish_at else privacy, "selfDeclaredMadeForKids": False,
                    "embeddable": True, "license": "youtube", "publicStatsViewable": True}
    if publish_at:
        status["publishAt"] = publish_at
    body = {
        "snippet": {"title": title[:100], "description": description[:4900], "tags": tags[:15],
                    "categoryId": CATEGORY.get(category, "24")},
        "status": status,
    }
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response, retries = None, 0
    while response is None:
        try:
            _, response = req.next_chunk()
        except HttpError as e:
            reason = _reason(e)
            if reason == "uploadLimitExceeded":
                raise UploadLimit("YouTube daily upload limit reached for this channel") from e
            if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                raise UploadLimit(f"YouTube API quota exceeded ({reason})") from e
            if e.resp.status in (401,) or reason in ("authError", "youtubeSignupRequired", "insufficientPermissions"):
                raise ActionNeeded(f"YouTube refused the login or channel ({reason or e.resp.status})") from e
            if e.resp.status in RETRIABLE_STATUS:
                retries += 1
            else:
                raise
        except (httplib2.HttpLib2Error, OSError) as e:
            log.warning("network trouble during upload: %s", e)
            retries += 1
        if response is None and retries:
            if retries > 8:
                raise RuntimeError("upload kept failing")
            time.sleep(min(2 ** retries, 60) + random.random())
    vid = response.get("id")
    if not vid:
        raise RuntimeError(f"unexpected upload response: {response}")
    return vid


def set_thumbnail(service, video_id: str, jpg: Path) -> bool:
    """Best effort. A refused thumbnail never blocks the upload."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    try:
        service.thumbnails().set(videoId=video_id,
                                 media_body=MediaFileUpload(str(jpg), mimetype="image/jpeg")).execute()
        return True
    except HttpError as e:
        log.warning("thumbnail not set for %s: %s %s", video_id, e.resp.status, _reason(e))
    except Exception as e:  # noqa: BLE001
        log.warning("thumbnail not set for %s: %s", video_id, e)
    return False
