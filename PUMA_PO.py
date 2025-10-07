#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PUMA_PO — unified (runner + logic)
User OAuth first (token.json or GMAIL_* env vars), no ADC fallback
"""

import argparse
import base64
import datetime as _dt
import io
import os
import re
import sys
from typing import List, Tuple, Dict, Any, Optional

# Optional PDF text extraction
try:
    import pdfplumber
except Exception:
    pdfplumber = None

# Google APIs
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2.credentials import Credentials

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

SPREADSHEET_ID = os.getenv("PUMA_SPREADSHEET_ID", "").strip()
PROJECTS_FOLDER_ID = os.getenv("PUMA_PO_DRIVE_FOLDER_ID", "").strip()
DEFAULT_LABEL_VISIBLE_NAME = "PUMA - PO"

TAB_MATCHED = "PO Matched"
TAB_UNMATCHED = "PO Unmatched"
MAKE_LINK_PUBLIC = False
DEBUG = True

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"

# -------------------------------------------------------------------
# UTILS
# -------------------------------------------------------------------

def dprint(*a, **k):
    if DEBUG:
        print(*a, **k)

# -------------------------------------------------------------------
# AUTH
# -------------------------------------------------------------------

def _creds_from_token_file() -> Optional[Credentials]:
    path = "/app/token.json"
    if os.path.exists(path):
        try:
            return Credentials.from_authorized_user_file(path, SCOPES)
        except Exception as e:
            print("[PO] WARNING: failed to read token.json:", repr(e))
    return None


def _creds_from_env() -> Optional[Credentials]:
    cid = os.getenv("GMAIL_CLIENT_ID", "").strip()
    cs = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    rt = os.getenv("GMAIL_REFRESH_TOKEN", "").strip()
    if cid and cs and rt:
        return Credentials(
            None,
            refresh_token=rt,
            client_id=cid,
            client_secret=cs,
            token_uri=TOKEN_URI,
            scopes=SCOPES,
        )
    return None


def get_services():
    # 1) Prefer token.json written by entrypoint
    creds = _creds_from_token_file()
    source = "token.json"

    # 2) Fallback to GMAIL_* environment vars
    if creds is None:
        creds = _creds_from_env()
        source = "env"

    # 3) Hard-fail if no valid user OAuth creds (no ADC)
    if creds is None:
        raise RuntimeError(
            "[PO] No user OAuth credentials. Provide /app/token.json OR "
            "GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN."
        )

    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # Show which mailbox we're actually using
    try:
        profile = gmail.users().getProfile(userId="me").execute()
        print(f"[PO] Auth source: {source} | Gmail profile:", profile.get("emailAddress"))
    except Exception as e:
        print("[PO] WARNING: could not read Gmail profile:", repr(e))

    return gmail, drive, sheets

# -------------------------------------------------------------------
# GMAIL HELPERS
# -------------------------------------------------------------------

_dash_re = re.compile(r"[–—−-]")

def _norm_label(s: str) -> str:
    s = _dash_re.sub("-", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def resolve_label_id(gmail, label_name: str) -> str:
    want = _norm_label(label_name)
    labs = gmail.users().labels().list(userId="me").execute().get("labels", [])
    # exact match
    for lab in labs:
        if _norm_label(lab.get("name", "")) == want:
            return lab["id"]
    # fallback partial
    for lab in labs:
        nm = _norm_label(lab.get("name", ""))
        if "puma" in nm and "po" in nm:
            return lab["id"]
    print("[PO] Available labels (normalized → original):",
          [(_norm_label(lab.get("name","")), lab.get("name","")) for lab in labs])
    print("[PO] Wanted (normalized):", want)
    raise ValueError(f"Label not found for '{label_name}' (normalized='{want}')")


def list_po_messages(gmail, include_read=False, days_back=None, label_name=DEFAULT_LABEL_VISIBLE_NAME, max_results=100):
    label_id = resolve_label_id(gmail, label_name)

    terms = ["has:attachment", "filename:pdf"]
    if not include_read:
        terms.append("is:unread")
    if days_back:
        terms.append(f"newer_than:{int(days_back)}d")
    q = " ".join(terms)

    dprint(f"[PO] Gmail search: {q} | label={label_name}")

    messages = []
    req = gmail.users().messages().list(userId="me", labelIds=[label_id], q=q, maxResults=max_results)
    while req is not None:
        resp = req.execute()
        messages.extend(resp.get("messages", []))
        req = gmail.users().messages().list_next(previous_request=req, previous_response=resp)

    dprint(f"[PO] Found {len(messages)} message(s).")
    return messages


def get_message(gmail, msg_id: str) -> Dict[str, Any]:
    return gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()


def iter_attachments(gmail, msg: Dict[str, Any]) -> List[Tuple[str, bytes, str]]:
    out = []
    payload = msg.get("payload", {})
    parts = payload.get("parts", []) or []
    for part in parts:
        filename = part.get("filename")
        body = part.get("body", {})
        mime = part.get("mimeType", "")
        att_id = body.get("attachmentId")
        data_b64 = body.get("data")
        if att_id:
            att = gmail.users().messages().attachments().get(
                userId="me", messageId=msg["id"], id=att_id
            ).execute()
            data_b64 = att.get("data")
        if filename and data_b64:
            data = base64.urlsafe_b64decode(data_b64.encode("utf-8"))
            out.append((filename, data, mime))
    return out


def msg_subject(msg: Dict[str, Any]) -> str:
    headers = msg.get("payload", {}).get("headers", [])
    for h in headers:
        if h.get("name", "").lower() == "subject":
            return h.get("value", "")
    return ""

# -------------------------------------------------------------------
# DRIVE & SHEETS HELPERS
# -------------------------------------------------------------------

def _ensure_subfolder(drive, parent_id: str, name: str) -> str:
    safe_name = name.replace("'", "\\'")
    q = (
        "mimeType = 'application/vnd.google-apps.folder' "
        f"and name = '{safe_name}' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    resp = drive.files().list(q=q, fields="files(id,name)").execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    folder = drive.files().create(body=meta, fields="id").execute()
    return folder["id"]


def upload_blob_to_drive(drive, folder_id: str, name: str, data: bytes, mimetype: str):
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=False)
    meta = {"name": name, "parents": [folder_id]}
    file = drive.files().create(body=meta, media_body=media, fields="id, webViewLink").execute()
    return file["id"], file.get("webViewLink", "")


def maybe_make_public(drive, file_id: str, should_share: bool):
    if not should_share:
        return
    try:
        perm = {"type": "anyone", "role": "reader"}
        drive.permissions().create(fileId=file_id, body=perm, fields="id").execute()
    except Exception as e:
        dprint(f"[Drive] Public sharing failed: {e}")


def append_rows(sheets, tab: str, values: List[List[Any]]):
    if not values:
        return
    rng = f"{tab}!A2"
    body = {"values": values}
    sheets.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=rng,
        valueInputOption="USER_ENTERED",
        body=body
    ).execute()

# -------------------------------------------------------------------
# PDF PARSER
# -------------------------------------------------------------------

def parse_po_pdf_with_tracker(pdf_bytes: bytes, cpn_list=None) -> Dict[str, Any]:
    text = ""
    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text += "\n" + (page.extract_text() or "")
        except Exception as e:
            dprint(f"[PDF] pdfplumber failed: {e}")
    if not text:
        return {"po_number": None, "items": []}

    po_number = None
    for pat in [
        r"\bPO(?:\s*#|[:\- ])\s*(\d{3,10})\b",
        r"\bPurchase\s*Order(?:\s*#|[:\- ])\s*(\d{3,10})\b",
        r"\bP\.?O\.?\s*(\d{3,10})\b",
    ]:
        m = re.search(pat, text, re.I)
        if m:
            po_number = m.group(1)
            break

    items = []
    for line in text.splitlines():
        line = line.strip()
        m = re.search(r"([A-Z0-9\-\._]{3,})\s+.*?\b(qty|quantity)\b[\s:]*([0-9]+)\b", line, re.I)
        if m:
            items.append({"sku": m.group(1), "qty": int(m.group(3))})
    return {"po_number": po_number, "items": items}

# -------------------------------------------------------------------
# MAIN FLOW
# -------------------------------------------------------------------

def fallback_po_from_filename(fname: str) -> Optional[str]:
    m = re.search(r"(?:PO[-_ ]?)?(\d{3,8})(?=\.pdf$|[^0-9])", fname, re.I)
    return f"PO-{m.group(1)}" if m else None


def process_message(gmail, drive, sheets, msg, project_root_folder_id: str=PROJECTS_FOLDER_ID, make_link_public: bool=MAKE_LINK_PUBLIC):
    subject = msg_subject(msg)
    msg_id = msg.get("id", "")
    when = _dt.datetime.now().isoformat(timespec="seconds")

    attachments = iter_attachments(gmail, msg)
    dprint(f"[PO] {msg_id} — '{subject}' — {len(attachments)} attachment(s)")

    unmatched_rows, matched_rows = [], []
    project_name = (subject or "PO")[:80]
    project_folder_id = _ensure_subfolder(drive, project_root_folder_id, project_name)

    for fname, data, mime in attachments:
        lower = (fname or "").lower()

        if not lower.endswith(".pdf"):
            file_id, link = upload_blob_to_drive(drive, project_folder_id, fname, data, mime or "application/octet-stream")
            maybe_make_public(drive, file_id, make_link_public)
            unmatched_rows.append([when, project_name, subject, "", fname, "", "Non-PDF attachment; skipped parse"])
            continue

        file_id, link = upload_blob_to_drive(drive, project_folder_id, fname, data, "application/pdf")
        maybe_make_public(drive, file_id, make_link_public)

        parsed = parse_po_pdf_with_tracker(data)
        po_number = parsed.get("po_number") or fallback_po_from_filename(fname)
        items = parsed.get("items", [])

        if not items:
            unmatched_rows.append([when, project_name, subject, po_number or "", fname, link, "PDF parsed but no items found"])
        else:
            for it in items:
                matched_rows.append([when, project_name, po_number or "", it.get("sku",""), it.get("qty",""), link, subject])

    if unmatched_rows:
        append_rows(sheets, TAB_UNMATCHED, unmatched_rows)
    if matched_rows:
        append_rows(sheets, TAB_MATCHED, matched_rows)

# -------------------------------------------------------------------
# RUN / CLI
# -------------------------------------------------------------------

def run(include_read=False, days_back=None, label=None):
    missing = []
    if not SPREADSHEET_ID: missing.append("PUMA_SPREADSHEET_ID")
    if not PROJECTS_FOLDER_ID: missing.append("PUMA_PO_DRIVE_FOLDER_ID")
    if missing:
        print(f"[PO] ERROR: Missing env var(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    gmail, drive, sheets = get_services()
    label_name = label or DEFAULT_LABEL_VISIBLE_NAME
    dprint(f"[PO] Starting run | include_read={include_read} | days_back={days_back} | label='{label_name}'")

    try:
        msgs = list_po_messages(gmail, include_read=include_read, days_back=days_back, label_name=label_name)
    except Exception as e:
        import traceback
        print("[PO] ERROR during list_po_messages() with label:", label_name)
        traceback.print_exc()
        return 1

    for m in msgs:
        try:
            msg = get_message(gmail, m["id"])
            process_message(gmail, drive, sheets, msg)
        except Exception:
            import traceback
            print("[PO] ERROR processing message id:", m.get("id"))
            traceback.print_exc()
            continue

    dprint("[PO] Completed OK")
    return 0


def main(argv=None):
    global DEBUG
    p = argparse.ArgumentParser()
    p.add_argument("--include-read", action="store_true")
    p.add_argument("--days-back", type=int, default=None)
    p.add_argument("--label", type=str, default=None)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--nodebug", action="store_true")
    args = p.parse_args(argv)
    if args.debug:
        DEBUG = True
    if args.nodebug:
        DEBUG = False
    return run(include_read=args.include_read, days_back=args.days_back, label=args.label)


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:
        import traceback
        print("[PO] FATAL:", repr(e))
        traceback.print_exc()
        rc = 1
    raise SystemExit(rc)
