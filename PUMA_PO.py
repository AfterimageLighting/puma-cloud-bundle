#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PUMA_PO — thread-aware PO ingest
- Strict label resolver
- Searches by THREADS so replies/unread don’t hide earlier PDFs
- Subject gates (--require-subject-po, --exclude-rfpo)
- Optional attachment gates (--no-attachment-filter, --no-pdf-filter)
- Shared Drive–safe (supportsAllDrives/corpora=allDrives)
- Drive subfolder per thread subject; uploads PDFs and parses basic PO info
- Matched/unmatched rows appended to Sheets
"""

import argparse, base64, datetime as _dt, io, os, re, sys
from typing import List, Tuple, Dict, Any, Optional

try:
    import pdfplumber
except Exception:
    pdfplumber = None

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2.credentials import Credentials

# ====== CONFIG VIA ENV ======
SPREADSHEET_ID = os.getenv("PUMA_SPREADSHEET_ID", "").strip()
PROJECTS_FOLDER_ID = os.getenv("PUMA_PO_DRIVE_FOLDER_ID", "").strip()
DEFAULT_LABEL_VISIBLE_NAME = "PUMA - PO"
TAB_MATCHED = "PO Matched"
TAB_UNMATCHED = "PO Unmatched"
MAKE_LINK_PUBLIC = False

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"

DEBUG = True
def dprint(*a, **k):
    if DEBUG:
        print(*a, **k)

# ====== AUTH ======
def _creds_from_token_file() -> Optional[Credentials]:
    p = "/app/token.json"
    if os.path.exists(p):
        try:
            return Credentials.from_authorized_user_file(p, SCOPES)
        except Exception as e:
            print("[PO] WARNING: failed to read token.json:", repr(e))
    return None

def _creds_from_env() -> Optional[Credentials]:
    cid = os.getenv("GMAIL_CLIENT_ID", "").strip()
    cs  = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    rt  = os.getenv("GMAIL_REFRESH_TOKEN", "").strip()
    if cid and cs and rt:
        return Credentials(
            None, refresh_token=rt, client_id=cid, client_secret=cs,
            token_uri=TOKEN_URI, scopes=SCOPES
        )
    return None

def get_services():
    creds = _creds_from_token_file(); source = "token.json"
    if creds is None:
        creds = _creds_from_env(); source = "env"
    if creds is None:
        raise RuntimeError("[PO] No Gmail OAuth available (token.json or env refresh token).")
    gmail  = build("gmail","v1", credentials=creds, cache_discovery=False)
    drive  = build("drive","v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets","v4", credentials=creds, cache_discovery=False)
    try:
        profile = gmail.users().getProfile(userId="me").execute()
        print(f"[PO] Auth source: {source} | Gmail profile:", profile.get("emailAddress"))
    except Exception as e:
        print("[PO] WARNING: could not read Gmail profile:", repr(e))
    return gmail, drive, sheets

# ====== HELPERS ======
_dash_re = re.compile(r"[–—−-]")
def _norm_label(s:str)->str:
    s = _dash_re.sub("-", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

def resolve_label_id(gmail, label_name: str) -> Tuple[str, str]:
    want = _norm_label(label_name)
    labs = gmail.users().labels().list(userId="me").execute().get("labels", [])
    for lab in labs:
        nm = lab.get("name","")
        if _norm_label(nm) == want:
            return lab["id"], nm
    print("[PO] Available labels:", [l.get("name","") for l in labs])
    raise ValueError(f"Label not found for '{label_name}'")

def msg_subject(msg: Dict[str,Any]) -> str:
    headers = msg.get("payload", {}).get("headers", [])
    for h in headers:
        if h.get("name","").lower() == "subject":
            return h.get("value","")
    return ""

def iter_message_attachments(gmail, msg: Dict[str,Any]) -> List[Tuple[str, bytes, str]]:
    out=[]
    payload = msg.get("payload",{}) or {}
    parts = payload.get("parts",[]) or []
    stack = parts[:]
    # also consider single-part messages
    if payload.get("filename"):
        stack.append(payload)
    while stack:
        part = stack.pop()
        if part.get("parts"):
            stack.extend(part["parts"])
            continue
        fname = part.get("filename")
        mime  = part.get("mimeType","")
        body  = part.get("body",{}) or {}
        data_b64 = body.get("data")
        att_id = body.get("attachmentId")
        if not fname:
            continue
        if att_id:
            att = gmail.users().messages().attachments().get(
                userId="me", messageId=msg["id"], id=att_id
            ).execute()
            data_b64 = att.get("data")
        if data_b64:
            try:
                data = base64.urlsafe_b64decode(data_b64)
            except Exception:
                data = base64.b64decode(data_b64)
            out.append((fname, data, mime))
    return out

# ====== DRIVE ======
def _ensure_subfolder(drive, parent_id: str, name: str) -> str:
    safe_name = name.replace("'", "\\'")
    q = (
        "mimeType = 'application/vnd.google-apps.folder' "
        f"and name = '{safe_name}' and '{parent_id}' in parents and trashed = false"
    )
    resp = drive.files().list(
        q=q, fields="files(id,name)",
        includeItemsFromAllDrives=True, supportsAllDrives=True, corpora="allDrives"
    ).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    meta = {
        "name": name, "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = drive.files().create(
        body=meta, fields="id", supportsAllDrives=True
    ).execute()
    return folder["id"]

def upload_blob_to_drive(drive, folder_id: str, name: str, data: bytes, mimetype: str):
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=False)
    meta = {"name": name, "parents": [folder_id]}
    file = drive.files().create(
        body=meta, media_body=media, fields="id, webViewLink",
        supportsAllDrives=True
    ).execute()
    return file["id"], file.get("webViewLink","")

def maybe_make_public(drive, file_id: str, share: bool):
    if not share: return
    try:
        drive.permissions().create(
            fileId=file_id, body={"type":"anyone","role":"reader"},
            fields="id", supportsAllDrives=True
        ).execute()
    except Exception as e:
        dprint("[Drive] Public share failed:", e)

# ====== SHEETS ======
def append_rows(sheets, tab: str, values: List[List[Any]]):
    if not values: return
    rng = f"{tab}!A2"
    body = {"values": values}
    sheets.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID, range=rng,
        valueInputOption="USER_ENTERED", body=body
    ).execute()

# ====== PDF PARSER ======
def parse_po_pdf(pdf_bytes: bytes) -> Dict[str,Any]:
    text=""
    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for p in pdf.pages:
                    text += "\n" + (p.extract_text() or "")
        except Exception as e:
            dprint("[PDF] pdfplumber failed:", e)
    if not text:
        return {"po_number": None, "items": []}

    po_number=None
    for pat in [r"\bPO(?:\s*#|[:\- ])\s*([A-Z0-9\-]{3,})\b",
                r"\bPurchase\s*Order(?:\s*#|[:\- ])\s*([A-Z0-9\-]{3,})\b",
                r"\bP\.?O\.?\s*([A-Z0-9\-]{3,})\b"]:
        m=re.search(pat, text, re.I)
        if m: po_number=m.group(1); break

    items=[]
    for line in text.splitlines():
        line=line.strip()
        m=re.search(r"([A-Z0-9\-\._]{3,})\s+.*?\b(qty|quantity)\b[:\s]*([0-9]+)\b", line, re.I)
        if m:
            items.append({"sku": m.group(1), "qty": int(m.group(3))})
    return {"po_number": po_number, "items": items}

def fallback_po_from_filename(fname:str)->Optional[str]:
    m=re.search(r"(?:PO[-_ ]?)?(\d{3,10})(?=\.pdf$|[^0-9])", fname, re.I)
    return f"PO-{m.group(1)}" if m else None

# ====== GMAIL (THREAD-AWARE) ======
def build_query(only_unread: bool, days_back: Optional[int],
                drop_attachment_filter: bool, drop_pdf_filter: bool,
                require_subject_po: bool, exclude_rfpo: bool) -> Optional[str]:
    terms=[]
    if only_unread:
        terms.append("is:unread")
    if days_back:
        terms.append(f"newer_than:{int(days_back)}d")
    if not drop_attachment_filter:
        terms.append("has:attachment")
        if not drop_pdf_filter:
            terms.append("filename:pdf")
    if require_subject_po:
        terms.append('subject:("purchase order" OR "PO-")')
    if exclude_rfpo:
        terms.append("-subject:rfpo")
    return " ".join(terms) if terms else None

def list_threads(gmail, label_id: str, q: Optional[str], max_results: int=500):
    threads=[]
    kwargs={"userId":"me","labelIds":[label_id],"maxResults":100}
    if q: kwargs["q"]=q
    req=gmail.users().threads().list(**kwargs)
    while req is not None and len(threads) < max_results:
        resp=req.execute()
        threads.extend(resp.get("threads",[]))
        req=gmail.users().threads().list_next(previous_request=req, previous_response=resp)
    return threads

# ====== MAIN FLOW ======
def process_thread(gmail, drive, sheets, thread: Dict[str,Any],
                   parent_folder_id: str, make_public: bool):
    # Load full thread
    t = gmail.users().threads().get(userId="me", id=thread["id"], format="full").execute()
    messages = t.get("messages", [])
    if not messages:
        return

    # Choose a stable folder name (first message subject)
    first_subject = msg_subject(messages[0]) or "PO"
    when = _dt.datetime.now().isoformat(timespec="seconds")
    project_folder_id = _ensure_subfolder(drive, parent_folder_id, first_subject[:80])

    matched_rows=[]; unmatched_rows=[]
    seen_files=set()

    for m in messages:
        subject = msg_subject(m)
        atts = iter_message_attachments(gmail, m)
        if not atts:
            continue

        for fname, data, mime in atts:
            if (m["id"], fname) in seen_files:
                continue
            seen_files.add((m["id"], fname))

            is_pdf = (fname or "").lower().endswith(".pdf")
            mimetype = "application/pdf" if is_pdf else (mime or "application/octet-stream")
            file_id, link = upload_blob_to_drive(drive, project_folder_id, fname, data, mimetype)
            maybe_make_public(drive, file_id, make_public)

            if not is_pdf:
                unmatched_rows.append([when, first_subject, subject, "", fname, link, "Non-PDF; skipped parse"])
                continue

            parsed = parse_po_pdf(data)
            po = parsed.get("po_number") or fallback_po_from_filename(fname) or ""
            items = parsed.get("items", [])
            if not items:
                unmatched_rows.append([when, first_subject, subject, po, fname, link, "PDF parsed; no items found"])
            else:
                for it in items:
                    matched_rows.append([when, first_subject, po, it.get("sku",""), it.get("qty",""), link, subject])

    if unmatched_rows:
        append_rows(sheets, TAB_UNMATCHED, unmatched_rows)
    if matched_rows:
        append_rows(sheets, TAB_MATCHED, matched_rows)

def run(
    label_name: str,
    days_back: Optional[int],
    only_unread: bool,
    drop_attachment_filter: bool,
    drop_pdf_filter: bool,
    require_subject_po: bool,
    exclude_rfpo: bool,
):
    # sanity
    missing=[]
    if not SPREADSHEET_ID: missing.append("PUMA_SPREADSHEET_ID")
    if not PROJECTS_FOLDER_ID: missing.append("PUMA_PO_DRIVE_FOLDER_ID")
    if missing:
        print("[PO] ERROR: Missing env:", ", ".join(missing), file=sys.stderr); return 2

    gmail, drive, sheets = get_services()

    # Resolve label
    label_id, label_actual = resolve_label_id(gmail, label_name)
    print(f"[PO] Starting run | label='{label_actual}' | days_back={days_back} | only_unread={only_unread}")

    q = build_query(
        only_unread=only_unread,
        days_back=days_back,
        drop_attachment_filter=drop_attachment_filter,
        drop_pdf_filter=drop_pdf_filter,
        require_subject_po=require_subject_po,
        exclude_rfpo=exclude_rfpo,
    )
    print(f"[PO] Gmail thread search: {q or '(no query)'} | label={label_actual}")

    threads = list_threads(gmail, label_id, q)
    print(f"[PO] Found {len(threads)} thread(s).")

    for th in threads:
        try:
            process_thread(gmail, drive, sheets, th, PROJECTS_FOLDER_ID, MAKE_LINK_PUBLIC)
        except Exception as e:
            import traceback
            print("[PO] ERROR processing thread id:", th.get("id"), "|", repr(e))
            traceback.print_exc()
            continue

    print("[PO] Completed OK")
    return 0

def main(argv=None):
    global DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default=DEFAULT_LABEL_VISIBLE_NAME)
    ap.add_argument("--days-back", type=int, default=None)
    ap.add_argument("--only-unread", action="store_true",
                    help="Limit to unread threads (default is all threads matching filters).")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--nodebug", action="store_true")
    # filters
    ap.add_argument("--no-attachment-filter", action="store_true",
                    help="Do not require has:attachment in search (thread parsing still skips non-PDFs).")
    ap.add_argument("--no-pdf-filter", action="store_true",
                    help="Require has:attachment but allow non-PDFs in search.")
    ap.add_argument("--require-subject-po", action="store_true",
                    help='Require subject to contain "Purchase Order" or "PO-".')
    ap.add_argument("--exclude-rfpo", action="store_true",
                    help="Exclude threads with RFPO in the subject.")
    args = ap.parse_args(argv)

    if args.debug: DEBUG=True
    if args.nodebug: DEBUG=False

    return run(
        label_name=args.label,
        days_back=args.days_back,
        only_unread=args.only_unread,
        drop_attachment_filter=args.no_attachment_filter,
        drop_pdf_filter=args.no_pdf_filter,
        require_subject_po=args.require_subject_po,
        exclude_rfpo=args.exclude_rfpo,
    )

if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:
        import traceback
        print("[PO] FATAL:", repr(e))
        traceback.print_exc()
        rc = 1
    raise SystemExit(rc)
