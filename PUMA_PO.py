#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PUMA_PO — unified (runner + logic) 2025-10-06
"""

import argparse
import base64
import datetime as _dt
import io
import os
import re
import sys
from typing import List, Tuple, Dict, Any, Optional

# Optional pdf text layer extraction
try:
    import pdfplumber
except Exception:
    pdfplumber = None

# Google APIs
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2.credentials import Credentials as UserCreds
import google.auth

# ------------------------- CONFIG ------------------------- #

SPREADSHEET_ID = os.getenv("PUMA_SPREADSHEET_ID", "").strip()
PROJECTS_FOLDER_ID = os.getenv("PUMA_PO_DRIVE_FOLDER_ID", "").strip()
DEFAULT_LABEL_VISIBLE_NAME = "PUMA - PO"  # use ASCII '-'

TAB_MATCHED = "PO Matched"
TAB_UNMATCHED = "PO Unmatched"
MAKE_LINK_PUBLIC = False  # set True only if policy allows

DEBUG = True

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"

# ------------------------- UTIL / AUTH ------------------------- #

def dprint(*a, **k):
    if DEBUG:
        print(*a, **k)

def build_user_credentials(scopes):
    cid = os.getenv("GMAIL_CLIENT_ID", "").strip()
    cs = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    rt = os.getenv("GMAIL_REFRESH_TOKEN", "").strip()
    if not (cid and cs and rt):
        return None
    return UserCreds(
        None,
        refresh_token=rt,
        client_id=cid,
        client_secret=cs,
        token_uri=TOKEN_URI,
        scopes=scopes,
    )

def get_services():
    # Prefer user OAuth (mailbox/Drive of human). Fallback to ADC for local/dev.
    creds = build_user_credentials(SCOPES)
    if creds is None:
        creds, _ = google.auth.default(scopes=SCOPES)
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return gmail, drive, sheets

# ------------------------- GMAIL HELPERS ------------------------- #

_dash_re = re.compile(r"[–—−-]")

def _norm_label(s: str) -> str:
    s = _dash_re.sub("-", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

def resolve_label_id(gmail, label_name: str) -> str:
    want = _norm_label(label_name)
    labs = gmail.users().labels().list(userId="me").execute().get("labels", [])
    # exact normalized match
    for lab in labs:
        if _norm_label(lab.get("name", "")) == want:
            return lab["id"]
    # soft fallback: contains both tokens
    for lab in labs:
        nm = _norm_label(lab.get("name", ""))
        if "puma" in nm and "po" in nm:
            return lab["id"]
    dprint("[PO] Available labels:", [lab.get("name", "") for lab in labs])
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

# ------------------------- DRIVE HELPERS ------------------------- #

def _ensure_subfolder(drive, parent_id: str, name: str) -> str:
    """Ensure a subfolder exists and return its id."""
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
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
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

# ------------------------- SHEETS HELPERS ------------------------- #

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

# ------------------------- PARSER ------------------------- #

def parse_po_pdf_with_tracker(pdf_bytes: bytes, cpn_list=None) -> Dict[str, Any]:
    text = ""
    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text() or ""
                    text += "\n" + t
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

# ------------------------- FLOW ------------------------- #

def _ln(x): 
    return (str(x or "")).strip().lower()

def fallback_po_from_filename(fname: str) -> Optional[str]:
    m = re.search(r"(?:PO[-_ ]?)?(\d{3,8})(?=\.pdf$|[^0-9])", fname, re.I)
    return f"PO-{m.group(1)}" if m else None

def process_message(gmail, drive, sheets, msg, project_root_folder_id: str=PROJECTS_FOLDER_ID, make_link_public: bool=MAKE_LINK_PUBLIC):
    subject = msg_subject(msg)
    msg_id = msg.get("id", "")
    when = _dt.datetime.now().isoformat(timespec="seconds")

    attachments = iter_attachments(gmail, msg)
    dprint(f"[PO] {msg_id} — '{subject}' — {len(attachments)} attachment(s)")

    unmatched_rows = []
    matched_rows = []  # shape this to your "PO Matched" tab layout

    project_name = (subject or "PO")[:80]
    project_folder_id = _ensure_subfolder(drive, project_root_folder_id, project_name)

    any_file_link = ""
    po_number_global = None

    for fname, data, mime in attachments:
        lower = (fname or "").lower()

        # Non-PDF: log as unmatched and (optionally) upload for record
        if not lower.endswith(".pdf"):
            file_id, link = upload_blob_to_drive(drive, project_folder_id, fname, data, mime or "application/octet-stream")
            maybe_make_public(drive, file_id, make_link_public)
            any_file_link = any_file_link or link
            unmatched_rows.append([when, project_name, subject, "", fname, "", "Non-PDF attachment; skipped parse"])
            continue

        # It's a PDF: upload first (so we always retain the doc), then parse
        file_id, link = upload_blob_to_drive(drive, project_folder_id, fname, data, "application/pdf")
        maybe_make_public(drive, file_id, make_link_public)
        any_file_link = any_file_link or link

        parsed = parse_po_pdf_with_tracker(data, cpn_list=None)
        po_number = parsed.get("po_number") or fallback_po_from_filename(fname)
        po_number_global = po_number_global or po_number

        items = parsed.get("items", [])
        if not items:
            unmatched_rows.append([when, project_name, subject, po_number or "", fname, link, "PDF parsed but no items found"])
        else:
            for it in items:
                matched_rows.append([when, project_name, po_number or "", it.get("sku",""), it.get("qty",""), link, subject])

    # Write rows
    if unmatched_rows:
        append_rows(sheets, TAB_UNMATCHED, unmatched_rows)
    if matched_rows:
        append_rows(sheets, TAB_MATCHED, matched_rows)

def run(include_read: bool=False, days_back: Optional[int]=None, label: Optional[str]=None):
    # env sanity
    missing = []
    if not SPREADSHEET_ID: missing.append("PUMA_SPREADSHEET_ID")
    if not PROJECTS_FOLDER_ID: missing.append("PUMA_PO_DRIVE_FOLDER_ID")
    if missing:
        print(f"[PO] ERROR: Missing required env var(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    gmail, drive, sheets = get_services()
    label_name = label or DEFAULT_LABEL_VISIBLE_NAME
    dprint(f"[PO] Starting run | include_read={include_read} | days_back={days_back} | label='{label_name}'")
    msgs = list_po_messages(gmail, include_read=include_read, days_back=days_back, label_name=label_name)
    for m in msgs:
        msg = get_message(gmail, m["id"])
        process_message(gmail, drive, sheets, msg)
    dprint("[PO] Completed OK")
    return 0

# ------------------------- CLI ------------------------- #

def main(argv=None):
    global DEBUG
    p = argparse.ArgumentParser()
    p.add_argument("--include-read", action="store_true", help="Include read emails (backfill mode)")
    p.add_argument("--days-back", type=int, default=None, help="Only fetch emails newer than N days")
    p.add_argument("--label", type=str, default=None, help="Override label name (default 'PUMA - PO')")
    p.add_argument("--debug", action="store_true", help="Enable verbose logging")
    p.add_argument("--nodebug", action="store_true", help="Disable verbose logging")
    args = p.parse_args(argv)

    if args.debug: DEBUG = True
    if args.nodebug: DEBUG = False

    return run(include_read=args.include_read, days_back=args.days_back, label=args.label)

if __name__ == "__main__":
    raise SystemExit(main())
