#!/usr/bin/env python3
"""Google Drive upload helpers for the CSV link builder.

Wraps OAuth, folder resolution, resumable uploads and link-sharing so the
Streamlit app in app.py stays about the UI. Uses the same OAuth client as
~/instagram-scraper (credentials.json / token.json live next to this file).
"""
import json
import mimetypes
import os
import random
import time

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# drive.file = access only to files this app creates. Least-privilege; the app
# can never see your other Drive files.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(_HERE, "credentials.json")
TOKEN_FILE = os.path.join(_HERE, "token.json")

# On a server there is no browser to run the consent flow in, so the grant is
# injected as an env var holding the contents of a token.json minted locally.
ENV_TOKEN = "GOOGLE_TOKEN_JSON"
ENV_CREDENTIALS = "GOOGLE_CREDENTIALS_JSON"


def _creds_from_env():
    """Build credentials from GOOGLE_TOKEN_JSON, or None if it is not set."""
    raw = os.environ.get(ENV_TOKEN, "").strip()
    if not raw:
        return None
    try:
        return Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    except Exception as exc:
        raise AuthError(
            "{} is set but unreadable ({}). Paste the full contents of a "
            "token.json generated locally.".format(ENV_TOKEN, exc)
        )

FOLDER_MIME = "application/vnd.google-apps.folder"
FOLDER_MIME_Q = "mimeType='{}'".format(FOLDER_MIME)
# Transient Drive errors worth retrying: rate limits and backend hiccups.
_RETRY_STATUS = {403, 429, 500, 502, 503, 504}


class AuthError(RuntimeError):
    """Raised when we cannot get a usable Drive credential without a browser."""


def token_state():
    """Describe the cached credential without triggering a browser flow.

    Returns (status, detail) where status is one of 'ok', 'refreshable',
    'missing', 'expired'.
    """
    try:
        env_creds = _creds_from_env()
    except AuthError as exc:
        return "missing", str(exc)
    if env_creds is not None:
        if env_creds.valid:
            return "ok", "Authorized from {}.".format(ENV_TOKEN)
        if env_creds.refresh_token:
            return "refreshable", "Token from {} will refresh on use.".format(ENV_TOKEN)
        return "expired", (
            "{} holds no usable refresh token — regenerate token.json locally "
            "and update the env var.".format(ENV_TOKEN)
        )

    if not os.path.exists(TOKEN_FILE):
        return "missing", "No token.json yet — you'll need to authorize once."
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    except Exception as exc:  # corrupt or wrong-scope token file
        return "missing", "token.json unreadable ({}).".format(exc)
    if creds.valid:
        return "ok", "Authorized."
    if creds.expired and creds.refresh_token:
        return "refreshable", "Access token expired; will refresh silently."
    return "expired", "Cached grant is no longer usable — re-authorize."


def get_service(allow_browser=True):
    """Return an authenticated Drive v3 service.

    Refreshes the cached token when possible. Only opens a browser when
    allow_browser is set, so the app can check auth state without hijacking
    the user's screen.
    """
    env_creds = _creds_from_env()
    if env_creds is not None:
        # Server path: refresh in memory, never write to the (ephemeral) disk
        # and never try to open a browser.
        if not env_creds.valid:
            if not env_creds.refresh_token:
                raise AuthError(
                    "{} has no refresh token.".format(ENV_TOKEN))
            try:
                env_creds.refresh(Request())
            except Exception as exc:
                raise AuthError(
                    "Could not refresh the Google grant ({}). If the OAuth "
                    "consent screen is still in Testing mode the refresh token "
                    "expires after 7 days — publish it, re-run the tool locally "
                    "to mint a fresh token.json, and update {}.".format(exc, ENV_TOKEN)
                )
        return build("drive", "v3", credentials=env_creds, cache_discovery=False)

    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except Exception:
                # Refresh tokens die after 7 days while the OAuth consent
                # screen is still in "Testing" — fall through to a fresh grant.
                creds = None
        if not refreshed and not (creds and creds.valid):
            if not allow_browser:
                raise AuthError("Not authorized yet — click Authorize first.")
            raw_client = os.environ.get(ENV_CREDENTIALS, "").strip()
            if raw_client:
                flow = InstalledAppFlow.from_client_config(json.loads(raw_client), SCOPES)
            elif os.path.exists(CREDENTIALS_FILE):
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            else:
                raise AuthError(
                    "Missing {}. Download an OAuth 'Desktop app' client ID from "
                    "Google Cloud Console and save it there.".format(CREDENTIALS_FILE)
                )
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _with_retry(request_fn, attempts=5):
    """Run a Drive request, retrying transient failures with backoff."""
    last = None
    for i in range(attempts):
        try:
            return request_fn()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status not in _RETRY_STATUS or i == attempts - 1:
                raise
            last = exc
            time.sleep((2 ** i) + random.random())
    raise last


def find_or_create_folder(service, name, parent_id=None):
    """Return the id of folder `name` under parent_id, creating it if absent.

    With the drive.file scope this only ever sees folders this app created, so
    re-running reuses last run's folder instead of making a duplicate.
    """
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    q = [FOLDER_MIME_Q, "trashed=false", "name='{}'".format(safe)]
    q.append("'{}' in parents".format(parent_id) if parent_id else "'root' in parents")
    res = _with_retry(lambda: service.files().list(
        q=" and ".join(q), spaces="drive", fields="files(id,name)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute())
    found = res.get("files", [])
    if found:
        return found[0]["id"]
    meta = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        meta["parents"] = [parent_id]
    folder = _with_retry(lambda: service.files().create(
        body=meta, fields="id", supportsAllDrives=True).execute())
    return folder["id"]


def resolve_folder_path(service, path, root_id=None):
    """Resolve a slash-separated folder path to a Drive folder id, creating
    each missing level. Empty path returns root_id (or None = My Drive root)."""
    parent = root_id
    for part in [p.strip() for p in path.split("/") if p.strip()]:
        parent = find_or_create_folder(service, part, parent)
    return parent


def list_folder_files(service, folder_id):
    """Return {name: file dict} for the non-trashed files directly in a folder.

    Used to skip re-uploading a file that is already there under the same name.
    """
    out = {}
    page = None
    while True:
        res = _with_retry(lambda: service.files().list(
            q="'{}' in parents and trashed=false".format(folder_id),
            spaces="drive",
            fields="nextPageToken, files(id,name,mimeType,size,webViewLink)",
            pageSize=1000, pageToken=page,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute())
        for f in res.get("files", []):
            if f.get("mimeType") != FOLDER_MIME:
                out[f["name"]] = f
        page = res.get("nextPageToken")
        if not page:
            break
    return out


def share_anyone_reader(service, file_id):
    """Grant 'anyone with the link can view' on a file."""
    _with_retry(lambda: service.permissions().create(
        fileId=file_id,
        body={"role": "reader", "type": "anyone"},
        supportsAllDrives=True,
    ).execute())


def upload_file(service, path, parent_id, share=True, progress_cb=None):
    """Upload one local file into parent_id. Returns the Drive file resource.

    Uses a resumable upload so large videos survive a flaky connection, and
    reports 0..1 progress through progress_cb.
    """
    mime, _ = mimetypes.guess_type(path)
    media = MediaFileUpload(path, mimetype=mime, resumable=True, chunksize=8 * 1024 * 1024)
    meta = {"name": os.path.basename(path), "parents": [parent_id]}
    request = service.files().create(
        body=meta, media_body=media,
        fields="id,name,mimeType,size,webViewLink,webContentLink",
        supportsAllDrives=True,
    )
    response = None
    while response is None:
        status, response = _with_retry(request.next_chunk)
        if status and progress_cb:
            progress_cb(status.progress())
    if progress_cb:
        progress_cb(1.0)
    if share:
        share_anyone_reader(service, response["id"])
    return response


def direct_link(file_id):
    """A link that streams/downloads the bytes rather than opening the Drive UI.

    Handy when the CSV feeds an ad tool or a sheet's IMAGE() formula.
    """
    return "https://drive.google.com/uc?export=download&id={}".format(file_id)


def preview_link(file_id):
    """Thumbnail-ish inline link usable in <img> tags and Sheets IMAGE()."""
    return "https://drive.google.com/thumbnail?id={}&sz=w1000".format(file_id)
