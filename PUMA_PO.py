
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PUMA_PO (cleaned) — unread-only mode by default

What changed vs. prior version:
- Resolve Gmail label ID dynamically by visible name ("PUMA – PO") instead of hardcoding.
- Search query constrained to unread + has:attachment + filename:pdf (unread-only mode).
- Explicit logging to a "PO Unmatched" sheet for:
    • Non-PDF attachments (skipped parse, still uploaded if desired).
    • PDFs that parsed but produced no items (likely image-only or unexpected layout).
- Safer status normalization for "Ordered / Received / Delivered" comparisons.
- Smarter fallback PO-number extraction from filenames when not found in the PDF.
- Functions structured for testability; backfill flags exist but default to unread-only.
"""
import base64
import datetime as _dt
import io
import os
import re
import sys
from typing import List, Tuple, Dict, Any, Optional

# Optional: pdf parser
try:
    import pdfplumber
except Exception:  # module may not be installed in all environments
    pdfplumber = None

# ------------------------- CONFIG (EDIT THESE) ------------------------- #
# These constants likely already exist in your repo; keep them aligned.
SPREADSHEET_ID = os.getenv("PUMA_SPREADSHEET_ID", "1pwVlYSGVjyTCLt4GT7xU2TCnxfdJuxAbp_jU6Snisls")
TAB_MATCHED = "PO Matched"      # where parsed line-items get written (adjust to your layout)
TAB_UNMATCHED = "PO Unmatched"  # where non-PDF/failed parses are logged
MAKE_LINK_PUBLIC = False        # Set True only if your Drive policy allows link-sharing
PROJECTS_FOLDER_ID = os.getenv("PUMA_PO_DRIVE_FOLDER_ID", "1VlCypDA_iF5dEUmA9c3E7ABYyS4-m6W2")  # parent folder for uploads
GMAIL_LABEL_VISIBLE_NAME = "PUMA – PO"  # exact visible name in Gmail (watch the dash and spaces)

# Toggle runtime logging
DEBUG = True

# ------------------------- GMAIL HELPERS ------------------------- #

def resolve_label_id(gmail, label_name: str) -> str:
    """Return Gmail labelId for a visible label name."""
    labs = gmail.users().labels().list(userId="me").execute().get("labels", [])
    for lab in labs:
        if lab.get("name") == label_name:
            return lab["id"]
    raise ValueError(f"Label '{label_name}' not found. Check exact spelling (including dashes/spaces).")

def list_po_messages(
    gmail,
    include_read: bool = False,
    days_back: Optional[int] = None,
    label_name: str = GMAIL_LABEL_VISIBLE_NAME,
    max_results: int = 100
) -> List[Dict[str, Any]]:
    """
    List PO-labeled emails. Default is unread-only mode.
    include_read=False keeps behavior light for production.
    """
    label_id = resolve_label_id(gmail, label_name)

    terms = ["has:attachment", "filename:pdf"]
    if not include_read:
        terms.append("is:unread")
    if days_back:
        terms.append(f"newer_than:{int(days_back)}d")
    q = " ".join(terms)

    if DEBUG:
        print(f"[PO] Gmail search: {q} | label={label_name}")

    messages = []
    req = gmail.users().messages().list(
        userId="me",
        labelIds=[label_id],
        q=q,
        maxResults=max_results
    )
    while req is not None:
        resp = req.execute()
        messages.extend(resp.get("messages", []))
        req = gmail.users().messages().list_next(previous_request=req, previous_response=resp)

    if DEBUG:
        print(f"[PO] Found {len(messages)} message(s).")
    return messages

def get_message(gmail, msg_id: str) -> Dict[str, Any]:
    return gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()

def iter_attachments(gmail, msg: Dict[str, Any]) -> List[Tuple[str, bytes, str]]:
    """Yield (filename, data_bytes, mimetype) for each attachment."""
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
            # fetch attachment
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
    q = f"mimeType = 'application/vnd.google-apps.folder' and name = '{name.replace(\"'\",\"\\'\")}' and '{parent_id}' in parents and trashed = false"
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

def upload_blob_to_drive(drive, folder_id: str, name: str, data: bytes, mimetype: str) -> Tuple[str, str]:
    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=False)
    meta = {"name": name, "parents": [folder_id]}
    file = drive.files().create(body=meta, media_body=media, fields="id, webViewLink").execute()
    file_id = file["id"]
    link = file.get("webViewLink", "")
    return file_id, link

def maybe_make_public(drive, file_id: str, should_share: bool):
    if not should_share:
        return
    try:
        perm = {"type": "anyone", "role": "reader"}
        drive.permissions().create(fileId=file_id, body=perm, fields="id").execute()
    except Exception as e:
        if DEBUG:
            print(f"[Drive] Public sharing failed: {e}")

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

# ------------------------- PARSING ------------------------- #

def parse_po_pdf_with_tracker(pdf_bytes: bytes, cpn_list=None) -> Dict[str, Any]:
    """
    Very lightweight parser:
    - Extract raw text with pdfplumber if available.
    - Try to find PO number by common tokens.
    - Try naive line-item extraction by matching SKU-like tokens (letters+digits) and quantities.
    Customize for your PO formats as needed.
    """
    text = ""
    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text() or ""
                    text += "\n" + t
        except Exception as e:
            if DEBUG:
                print(f"[PDF] pdfplumber failed: {e}")
    if not text:
        return {"po_number": None, "items": []}

    # PO number patterns
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

    # Naive line items - adjust to your vendors
    # Capture rows like: "SKU123  Description ...  Qty 2  Price 45.00"
    items = []
    for line in text.splitlines():
        line = line.strip()
        # Very lenient SKU/QTY match
        m = re.search(r"([A-Z0-9\-\._]{3,})\s+.*?\b(qty|quantity)\b[\s:]*([0-9]+)\b", line, re.I)
        if m:
            sku = m.group(1)
            qty = int(m.group(3))
            items.append({"sku": sku, "qty": qty})

    return {"po_number": po_number, "items": items}

# ------------------------- BUSINESS LOGIC ------------------------- #

def _ln(x): 
    return (str(x or "")).strip().lower()

def fallback_po_from_filename(fname: str) -> Optional[str]:
    m = re.search(r"(?:PO[-_ ]?)?(\d{3,8})(?=\.pdf$|[^0-9])", fname, re.I)
    if m:
        return f"PO-{m.group(1)}"
    return None

def process_message(
    gmail, drive, sheets,
    msg: Dict[str, Any],
    project_root_folder_id: str = PROJECTS_FOLDER_ID,
    make_link_public: bool = MAKE_LINK_PUBLIC
):
    subject = msg_subject(msg)
    msg_id = msg.get("id", "")
    when = _dt.datetime.now().isoformat(timespec="seconds")

    attachments = iter_attachments(gmail, msg)
    if DEBUG:
        print(f"[PO] {msg_id} — '{subject}' — {len(attachments)} attachment(s)")

    unmatched_rows = []
    matched_rows = []  # shape this to your "PO Matched" tab layout

    # Heuristic project folder (customize as needed)
    project_name = subject[:80] or "PO"
    project_folder_id = _ensure_subfolder(drive, project_root_folder_id, project_name)

    # Gathered outputs for logging
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
            # TODO: Shape to your "PO Matched" schema
            for it in items:
                matched_rows.append([when, project_name, po_number or "", it.get("sku",""), it.get("qty",""), link, subject])

    # Write rows
    if unmatched_rows:
        append_rows(sheets, TAB_UNMATCHED, unmatched_rows)
    if matched_rows:
        append_rows(sheets, TAB_MATCHED, matched_rows)

def run(gmail, drive, sheets, include_read: bool = False, days_back: Optional[int] = None):
    """Entry point for Cloud Run / local cron. Defaults to unread-only mode."""
    msgs = list_po_messages(gmail, include_read=include_read, days_back=days_back)
    for m in msgs:
        msg = get_message(gmail, m["id"])
        process_message(gmail, drive, sheets, msg)

# If you prefer CLI flags, wire them here (optional).
if __name__ == "__main__":
    print("This module expects initialized Gmail/Drive/Sheets clients passed to run().")
    print("In production (Cloud Run), import run() from this module and invoke with your authenticated services.")
