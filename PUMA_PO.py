#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, io, re, argparse, logging, datetime as _dt
from typing import Dict, Any, Iterable, Tuple, List

# ---------- Google API ----------
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.auth.exceptions import RefreshError
from puma_project_resolver import resolve_existing_tracker

# (lightweight parsing helper – keep whatever you already use)
try:
    import pdfplumber
except Exception:
    pdfplumber = None


# ===================== CONFIG / CONSTANTS =====================

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

DEFAULT_LABEL_VISIBLE_NAME = os.getenv("PUMA_PO_LABEL_NAME", "PUMA/PUMA - PO").strip()
TAB_MATCHED   = "PO Matched"
TAB_UNMATCHED = "PO Unmatched"

# Drive folder + sheet id from env (with sanitizer)
PROJECTS_FOLDER_ID = os.getenv("PUMA_PO_DRIVE_FOLDER_ID", "")
RAW_SPREADSHEET_ID = os.getenv("PUMA_SPREADSHEET_ID", "")
# Remove any stray angle brackets or whitespace that may have been pasted
SPREADSHEET_ID     = re.sub(r"[<>\s]", "", RAW_SPREADSHEET_ID)
MAKE_LINK_PUBLIC   = False

DEBUG = False

def dprint(*a, **k):
    if DEBUG:
        print(*a, **k)


# ===================== AUTH / SERVICES =====================

def _whoami(creds: Credentials) -> str:
    try:
        gm = build("gmail", "v1", credentials=creds)
        me = gm.users().getProfile(userId="me").execute()
        return me.get("emailAddress", "?")
    except Exception:
        return "?"


def _build_services(creds: Credentials):
    gmail  = build("gmail",  "v1", credentials=creds, cache_discovery=False)
    drive  = build("drive",  "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return gmail, drive, sheets


def get_services() -> Tuple[Any, Any, Any]:
    """
    Prefer ENV client+refresh creds.
    Fallback to token.json only if ENV is absent (legacy).
    """
    cid  = os.getenv("GMAIL_CLIENT_ID")
    csec = os.getenv("GMAIL_CLIENT_SECRET")
    rtok = os.getenv("GMAIL_REFRESH_TOKEN")

    if cid and csec and rtok:
        creds = Credentials(
            None,
            refresh_token=rtok,
            client_id=cid,
            client_secret=csec,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        print("[PO] Auth source: env | Gmail profile:", _whoami(creds))
        return _build_services(creds)

    # fallback (legacy)
    try:
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        print("[PO] Auth source: token.json | Gmail profile:", _whoami(creds))
        return _build_services(creds)
    except Exception as e:
        raise SystemExit(f"[PO] FATAL: could not load credentials ({e})")


# ===================== PRE-FLIGHT CHECKS =====================

def assert_sheet_ready(sheets, spreadsheet_id: str, tab_names: List[str]):
    """Fail fast with clear errors if spreadsheet or tabs are wrong."""
    try:
        meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        titles = {s.get("properties", {}).get("title") for s in meta.get("sheets", [])}
        for tab in tab_names:
            if tab not in titles:
                raise RuntimeError(f"Tab not found: '{tab}'. Existing tabs: {sorted(titles)}")
        logging.info(f"[PO] Sheets preflight OK for {spreadsheet_id[-8:]} tabs={tab_names}")
    except Exception as e:
        logging.error(f"[PO] Sheets preflight FAILED for {spreadsheet_id[-8:]}: {e}")
        raise


# ===================== GMAIL HELPERS =====================

def resolve_label_id(gmail, label_name: str) -> Tuple[str, str]:
    """Accepts either 'PUMA/PUMA - PO' (path) or visible name 'PUMA - PO'."""
    labs = gmail.users().labels().list(userId="me").execute().get("labels", [])
    names = [l.get("name") for l in labs]
    # exact match first
    for l in labs:
        if l.get("name") == label_name:
            return l["id"], l["name"]
    # if user passed visible child (e.g., 'PUMA - PO'), try to find path 'PUMA/<child>'
    if "/" not in label_name:
        for l in labs:
            n = l.get("name", "")
            if n.endswith("/" + label_name) or n == label_name:
                return l["id"], l["name"]
    raise ValueError(f"Label not found for '{label_name}' (have: {names})")


def msg_subject(msg: Dict[str, Any]) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == "subject":
            return h.get("value", "")
    return ""


def iter_message_attachments(gmail, message: Dict[str, Any]) -> Iterable[Tuple[str, bytes, str]]:
    """
    Yield (filename, data, mime) for each attachment in a single message.
    """
    def _walk_parts(p):
        yield p
        for c in (p.get("parts") or []):
            yield from _walk_parts(c)

    payload = message.get("payload", {})
    for part in _walk_parts(payload):
        body = part.get("body", {})
        att_id = body.get("attachmentId")
        if not att_id:
            continue
        data = gmail.users().messages().attachments().get(
            userId="me", messageId=message["id"], id=att_id
        ).execute().get("data")
        import base64
        raw = base64.urlsafe_b64decode(data.encode("utf-8"))
        fname = None
        mime = part.get("mimeType")
        for h in (part.get("headers") or []):
            if h.get("name", "").lower() == "content-disposition" and "filename=" in h.get("value", ""):
                # crude extraction
                v = h.get("value")
                idx = v.find("filename=")
                if idx >= 0:
                    fname = v[idx+9:].strip('"; ')
        if not fname:
            fname = part.get("filename") or "attachment.bin"
        yield fname, raw, mime or "application/octet-stream"


# ===================== DRIVE / SHEETS HELPERS =====================

def _ensure_subfolder(drive, parent_id: str, name: str) -> str:
    # Find existing subfolder by name under parent
    safe_name = name.replace("'", "\\'")
    q = (
        f"name = '{safe_name}' and "
        f"mimeType = 'application/vnd.google-apps.folder' and "
        f"'{parent_id}' in parents and "
        f"trashed = false"
    )
    resp = drive.files().list(
        q=q,
        spaces="drive",
        fields="files(id,name)",
        pageSize=10,
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]

    # Create it if missing
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    
    folder = drive.files().create(
        body=meta,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return folder["id"]


def upload_blob_to_drive(drive, parent_id: str, name: str, data: bytes, mime: str) -> Tuple[str, str]:
    meta = {"name": name, "parents": [parent_id]}
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
    f = drive.files().create(
        body=meta, media_body=media, fields="id,webViewLink,webContentLink",
        supportsAllDrives=True
    ).execute()
    link = f.get("webViewLink") or f.get("webContentLink") or ""
    return f["id"], link


def maybe_make_public(drive, file_id: str, public: bool = False):
    if not public:
        return
    try:
        drive.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
            supportsAllDrives=True
        ).execute()
    except Exception as e:
        dprint("make_public failed:", e)


def append_rows(sheets, tab: str, rows: List[List[Any]]):
    if not rows:
        return
    body = {"values": rows}
    sheets.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{tab}!A:Z",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()


# ===================== VERY LIGHT PO PARSER (leave as-is if you have your own) =====================

def parse_po_pdf(data: bytes) -> Dict[str, Any]:
    """
    Return {'po_number': str or None, 'items': [ {sku, qty}, ... ]} if possible.
    We keep it light — you likely already have your own. This is harmless fallback.
    """
    out = {"po_number": None, "items": []}
    if not pdfplumber:
        return out
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        import re as _re
        m = _re.search(r"\bPO[-\s#]?\s*(\d{3,6})\b", text, flags=_re.I)
        if m:
            out["po_number"] = f"PO-{m.group(1)}"
    except Exception:
        pass
    return out


def normalize_po_number(po: str, fname: str) -> str:
    import re as _re
    if po:
        return po
    m = _re.search(r"\bPO[-_ ]?(\d{3,6})\b", fname, flags=_re.I)
    if m:
        return f"PO-{m.group(1)}"
    return None


def project_from_subject(subject: str) -> str:
    """Extract the likely project portion of a QuickBooks PO subject."""
    s = subject or ""
    s = re.sub(r"^(?:re|fwd?|fw)\s*:\s*", "", s, flags=re.I).strip()
    # Common format: "Purchase Order PO-2023 - Compton"
    if " - " in s:
        return s.split(" - ")[-1].strip()
    # Remove leading purchase-order wording / PO number when possible.
    s = re.sub(r"(?i)^purchase\s+order\s*", "", s).strip()
    s = re.sub(r"(?i)^PO[-_ ]?\d+\s*", "", s).strip(" -:")
    return s


# ===================== CORE PROCESSING =====================

def process_thread(gmail, drive, sheets, thread: Dict[str,Any],
                   parent_folder_id: str, make_public: bool,
                   mark_read: bool=False, audit_nonpdf: bool=False):
    t = gmail.users().threads().get(userId="me", id=thread["id"], format="full").execute()
    messages = t.get("messages", [])
    if not messages:
        return

    first_subject = msg_subject(messages[0]) or "PO"
    project_guess = project_from_subject(first_subject)
    when = _dt.datetime.now().isoformat(timespec="seconds")

    resolution = resolve_existing_tracker(
        sheets,
        SPREADSHEET_ID,
        project_guess,
    )

    if not resolution.confirmed:
        # PO cloud processing is a filing/logging step. It must not create a
        # project folder from an unconfirmed email subject.
        append_rows(
            sheets,
            TAB_UNMATCHED,
            [[
                when,
                project_guess,
                first_subject,
                "",
                "",
                "",
                f"Project resolution {resolution.status} ({resolution.method}); no upload performed",
            ]]
        )
        dprint(
            "[PO] Project unresolved:",
            project_guess,
            resolution.status,
            resolution.suggestions,
        )
        return

    project_name = resolution.canonical_project
    project_folder_id = _ensure_subfolder(drive, parent_folder_id, project_name[:80])

    matched_rows, unmatched_rows = [], []
    seen_pdf_keys=set()
    any_pdf_success=False

    for m in messages:
        subject = msg_subject(m)
        for fname, data, mime in iter_message_attachments(gmail, m):
            is_pdf = (fname or "").lower().endswith(".pdf")
            if not is_pdf:
                if audit_nonpdf:
                    unmatched_rows.append([when, project_name, subject, "", fname, "", "Non-PDF; skipped"])
                continue

            key=(len(data), hash(data[:4096]))
            if key in seen_pdf_keys: 
                continue
            seen_pdf_keys.add(key)

            parsed = parse_po_pdf(data)
            po_num = normalize_po_number(parsed.get("po_number"), fname) or "PO-UNKNOWN"
            save_name = f"{po_num}.pdf"

            file_id, link = upload_blob_to_drive(drive, project_folder_id, save_name, data, "application/pdf")
            maybe_make_public(drive, file_id, make_public)

            items = parsed.get("items", [])
            # === mark thread "handled" if ANY PDF saved ===
            any_pdf_success = True

            if items:
                for it in items:
                    matched_rows.append([when, project_name, po_num, it.get("sku",""), it.get("qty",""), link, subject])
            else:
                unmatched_rows.append([when, project_name, subject, po_num, save_name, link, "PDF parsed; no items found"])

    if unmatched_rows: append_rows(sheets, TAB_UNMATCHED, unmatched_rows)
    if matched_rows:   append_rows(sheets, TAB_MATCHED,   matched_rows)

    if mark_read and any_pdf_success:
        try:
            # Remove UNREAD label at thread level
            gmail.users().threads().modify(
                userId="me", id=thread["id"], body={"removeLabelIds":["UNREAD"]}
            ).execute()
        except Exception as e:
            dprint("[PO] Mark-read failed:", e)


def build_query(only_unread: bool, days_back: int,
                no_attachment_filter: bool, no_pdf_filter: bool,
                require_subject_po: bool, exclude_rfpo: bool) -> str:
    q_parts = []
    if only_unread:
        q_parts.append("is:unread")
    if days_back:
        q_parts.append(f"newer_than:{days_back}d")

    # attachment gates
    if not no_attachment_filter:
        if no_pdf_filter:
            q_parts.append("has:attachment")
        else:
            q_parts.append("has:attachment filename:pdf")

    # subject gates
    if require_subject_po:
        q_parts.append('subject:("purchase order" OR "PO-")')

    if exclude_rfpo:
        q_parts.append(" -subject:rfpo")

    return " ".join(q_parts).strip()


def run(args) -> int:
    if not PROJECTS_FOLDER_ID or not SPREADSHEET_ID:
        raise SystemExit("[PO] FATAL: Missing PUMA_PO_DRIVE_FOLDER_ID or PUMA_SPREADSHEET_ID env")

    gmail, drive, sheets = get_services()

    # Preflight the sheet & tabs before doing any work
    assert_sheet_ready(sheets, SPREADSHEET_ID, [TAB_UNMATCHED, TAB_MATCHED])

    try:
        label_id, label_actual = resolve_label_id(gmail, args.label)
    except Exception as e:
        raise SystemExit(f"[PO] FATAL: {e}")

    q = build_query(
        only_unread=args.only_unread,
        days_back=args.days_back,
        no_attachment_filter=args.no_attachment_filter,
        no_pdf_filter=args.no_pdf_filter,
        require_subject_po=args.require_subject_po,
        exclude_rfpo=args.exclude_rfpo,
    )

    print(f"[PO] Starting run | label='{label_actual}' | days_back={args.days_back} | only_unread={args.only_unread}")
    print(f"[PO] Gmail thread search: {q or '(no query)'} | label={label_actual}")
    print(f"[PO] Using spreadsheet ...{SPREADSHEET_ID[-8:]} (sanitized from env)")

    res = gmail.users().threads().list(userId="me", q=q, labelIds=[label_id]).execute() or {}
    threads = res.get("threads", []) or []
    print(f"[PO] Found {len(threads)} thread(s).")

    for th in threads:
        try:
            process_thread(gmail, drive, sheets, th, PROJECTS_FOLDER_ID, MAKE_LINK_PUBLIC,
                           mark_read=args.mark_read, audit_nonpdf=args.audit_nonpdf)
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
    ap.add_argument("--only-unread", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--nodebug", action="store_true")

    ap.add_argument("--no-attachment-filter", action="store_true",
                    help="Do not require has:attachment in search.")
    ap.add_argument("--no-pdf-filter", action="store_true",
                    help="Require has:attachment but allow non-PDFs in search.")
    ap.add_argument("--require-subject-po", action="store_true",
                    help='Require subject to contain "purchase order" or "PO-".')
    ap.add_argument("--exclude-rfpo", action="store_true",
                    help="Exclude threads with RFPO in the subject.")
    ap.add_argument("--mark-read", action="store_true",
                    help="Remove UNREAD from thread after at least one PDF processed.")
    ap.add_argument("--audit-nonpdf", action="store_true",
                    help="Log non-PDF attachments to Unmatched tab (off by default).")
    args = ap.parse_args(argv)

    if args.debug: DEBUG = True
    if args.nodebug: DEBUG = False

    logging.basicConfig(level=logging.INFO)

    try:
        return run(args)
    except RefreshError as e:
        print("[PO] FATAL:", repr(e))
        return 1


if __name__ == "__main__":
    rc = 0
    try:
        rc = main()
    except Exception as e:
        import traceback
        print("[PO] FATAL:", repr(e))
        traceback.print_exc()
        rc = 1
    raise SystemExit(rc)
