"""Shared Google OAuth loading for PUMA processors.

Cloud/Test is strictly non-interactive. Local/manual runs may use the browser
flow only when no refreshable credential source is available.
"""

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from puma_runtime_config import TEST_MODE


def _is_cloud_runtime() -> bool:
    return bool(os.getenv("K_SERVICE"))


def load_google_user_credentials(
    scopes,
    token_file: str = "token.json",
    client_secret_file: str = "client_secrets.json",
):
    cid = os.getenv("GMAIL_CLIENT_ID", "").strip()
    csec = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    rtok = os.getenv("GMAIL_REFRESH_TOKEN", "").strip()

    if cid and csec and rtok:
        creds = Credentials(
            None,
            refresh_token=rtok,
            client_id=cid,
            client_secret=csec,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=list(scopes),
        )
        creds.refresh(Request())
        return creds

    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, list(scopes))

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_file, "w", encoding="utf-8") as token:
            token.write(creds.to_json())
        return creds

    if TEST_MODE or _is_cloud_runtime():
        raise RuntimeError(
            "Cloud/Test requires non-interactive Google OAuth credentials "
            "(GMAIL_* refresh-token env vars or a refreshable token.json)."
        )

    if not os.path.exists(client_secret_file):
        raise RuntimeError(
            f"Google OAuth client file not found: {client_secret_file}"
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        client_secret_file,
        list(scopes),
    )
    creds = flow.run_local_server(port=0)
    with open(token_file, "w", encoding="utf-8") as token:
        token.write(creds.to_json())
    return creds
