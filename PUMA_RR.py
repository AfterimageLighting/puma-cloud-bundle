# PUMA6_RR.py — Receiving Report -> Drive upload + Status="Received" + Date Received hyperlink (Col J)
# Changes vs PUMA5:
#  - PDF parser handles multi-line rows (PN/desc on one line, "N unit(s)" on the next)
#  - Only writes to Status and Date Received cells (no full-row overwrite), preserving PO hyperlinks in column H
#  - Keeps strict Gmail query (subject:"Receiving Report"), tab resolution, dual-key PN matching, and "Date Received" header

import os, re, io, sys, base64, datetime
from typing import List, Tuple, Dict
from puma_project_resolver import resolve_subject_to_existing_tracker
from puma_runtime_config import test_safe_env
from puma_google_auth import load_google_user_credentials
from puma_status import can_advance_status, normalize_status

SPREADSHEET_ID = test_safe_env('PUMA_SPREADSHEET_ID', '1pwVlYSGVjyTCLt4GT7xU2TCnxfdJuxAbp_jU6Snisls')
RR_BASE_FOLDER_ID = test_safe_env('PUMA_RR_BASE_FOLDER_ID', '1ZpATQXv7owmljEpLYrTvpuHgNU8nSPxC')
MAKE_LINK_PUBLIC = False
DEBUG = True
if "--nodebug" in sys.argv:
    DEBUG = False

def resolve_label_id(gmail, label_name: str) -> str:
    """Return Gmail labelId for a visible label name."""
    labs = gmail.users().labels().list(userId="me").execute().get("labels", [])
    for lab in labs:
        if lab.get("name") == label_name:
            return lab["id"]
    raise ValueError(f"Label '{label_name}' not found.")

# ---- deps ----
import pdfplumber

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

# ---------- Auth ----------
def setup_services():
    try:
        creds = load_google_user_credentials(SCOPES)
        return (
            build('gmail', 'v1', credentials=creds),
            build('sheets', 'v4', credentials=creds),
            build('drive', 'v3', credentials=creds),
        )
    except Exception as e:
        print(f"Auth error: {e}")
        return None, None, None

# ---------- Gmail ----------
def list_rr_messages(gmail):
    """Return unread Receiving Report emails under the PUMA - RR label."""
    msgs = []
    req = gmail.users().messages().list(
        userId="me",
        labelIds=[resolve_label_id(gmail, test_safe_env('PUMA_RR_LABEL_NAME', 'PUMA - RR'))],
        q="is:unread has:attachment",
        maxResults=50
    )
    while req is not None:
        resp = req.execute()
        msgs.extend(resp.get("messages", []))
        req = gmail.users().messages().list_next(previous_request=req, previous_response=resp)
    if DEBUG: 
        print(f"[RR] Found {len(msgs)} email(s).")
    return msgs

def get_subject_attachments_timestamp(gmail, msg_id):
    msg = gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()
    subject = next((h["value"] for h in msg["payload"].get("headers", []) if h["name"].lower()=="subject"), "")
    internal_ms = int(msg.get("internalDate", "0"))
    email_dt = datetime.datetime.fromtimestamp(internal_ms/1000.0)
    atts = []
    def walk(p):
        if "parts" in p:
            for c in p["parts"]: yield from walk(c)
        else: yield p
    for p in walk(msg["payload"]):
        b = p.get("body", {})
        if b.get("attachmentId"):
            a = gmail.users().messages().attachments().get(userId="me", messageId=msg_id, id=b["attachmentId"]).execute()
            atts.append((p.get("filename") or "attachment.pdf", base64.urlsafe_b64decode(a["data"])))
    return subject, atts, email_dt

def mark_read(gmail, msg_id):
    gmail.users().messages().modify(userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}).execute()

# ---------- Subject / Project ----------
DASH_RX = re.compile(r'[\u2010-\u2015]')  # normalize fancy dashes to '-'
def _clean_project_tokens(name: str) -> str:
    # Keep "Residence"; only remove "Project/Proj."
    name = re.sub(r'(?i)\b(project|proj\.?)\b', '', name)
    name = re.sub(r'\s+', ' ', name).strip(' -–:— ')
    return name.strip()

def project_from_subject(subject: str) -> str:
    s = DASH_RX.sub('-', subject or "").strip()
    m = re.search(r'^(.*?)\s*[-:]\s*(?:receiving\s*report|rr)\b', s, re.I)
    if m and m.group(1).strip(): return _clean_project_tokens(m.group(1))
    m = re.search(r'(?:receiving\s*report|rr)\b\s*[-:]\s*(.*)$', s, re.I)
    if m and m.group(1).strip(): return _clean_project_tokens(m.group(1))
    s = re.sub(r'(?i)\breceiving\s*report\b', '', s)
    s = re.sub(r'(?i)\brr\b', '', s)
    s = re.sub(r'[\(\[].*?[\)\]]', '', s)
    return _clean_project_tokens(s) or subject

def _fetch_sheets_meta(sheets):
    return sheets.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute().get("sheets", [])

def get_tracker_snapshot(sheets, tab_title):
    """Load one CONFIRMED existing tracker. Never creates or fuzzy-selects a tab."""
    titles = {
        sh["properties"]["title"]
        for sh in _fetch_sheets_meta(sheets)
    }
    if tab_title not in titles:
        raise RuntimeError(f"Confirmed tracker no longer exists: {tab_title}")

    hdr = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{tab_title}'!1:1"
    ).execute().get("values", [[]])[0]

    idx = {}
    def _lnorm(x): return (str(x or "")).strip().lower()
    for i, h in enumerate(hdr):
        t = _lnorm(h)
        if t in {"type","item type","category"}: idx["Type"] = i
        elif t in {"manufacturer","mfr","mfg","brand","vendor"}: idx["Manufacturer"] = i
        elif t in {"part number","part #","part no","item #","sku","item","item no"}: idx["Part Number"] = i
        elif t in {"qty","quantity","qnty","qty ordered","qty/ea"}: idx["Quantity"] = i
        elif t == "status": idx["Status"] = i
        elif "po number" in t or t == "po #": idx["PO Number"] = i
        elif "estimated ship" in t or "esd" in t or "ship date" in t: idx["ESD"] = i
        elif (("date" in t and "receive" in t) or t.startswith("receiving rep") or t.startswith("receiving report") or t == "rr" or "rr link" in t):
            idx["RR Link"] = i

    res = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{tab_title}'!A2:J"
    ).execute()
    values = res.get("values", [])
    pn_col = idx.get("Part Number")
    part_to_rows: Dict[str, List[int]] = {}
    for r, row in enumerate(values):
        pn = (str(row[pn_col]).strip() if pn_col is not None and pn_col < len(row) else "")
        if pn:
            part_to_rows.setdefault(pn, []).append(r)

    if DEBUG:
        print(f"[RR] Using confirmed tracker tab: {tab_title}")
        print(f"[RR] Tracker dictionary loaded: {len(part_to_rows)} PNs")
    return tab_title, idx, values, part_to_rows

def ensure_unmatched_tab(sheets):
    tab = "RR Unmatched"
    meta = _fetch_sheets_meta(sheets)
    titles = {s['properties']['title'] for s in meta}
    if tab not in titles:
        sheets.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID,
            body={'requests':[{'addSheet':{'properties':{'title':tab}}}]}).execute()
        sheets.spreadsheets().values().update(spreadsheetId=SPREADSHEET_ID, range=f"{tab}!A1:G1",
            valueInputOption='RAW',
            body={'values': [["Timestamp","Project","Email Subject","Qty","Part Number","Note","File Link"]]}).execute()
    return tab

# ---------- Drive ----------
def _sanitize(name: str) -> str: return re.sub(r'[\\/]+', '-', name).strip()[:120]
def get_or_create_child_folder(drive, parent_id: str, name: str) -> str:
    name = _sanitize(name)
    q = f"mimeType='application/vnd.google-apps.folder' and trashed=false and name='{name}' and '{parent_id}' in parents"
    resp = drive.files().list(q=q, fields="files(id,name)", supportsAllDrives=True, includeItemsFromAllDrives=True, pageSize=10).execute()
    files = resp.get("files", [])
    if files: return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    folder = drive.files().create(body=meta, fields="id", supportsAllDrives=True).execute()
    return folder["id"]
def upload_pdf_to_drive(drive, parent_id: str, filename: str, data: bytes, make_public=False):
    body = {"name": _sanitize(filename), "parents": [parent_id], "mimeType": "application/pdf"}
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/pdf", resumable=False)
    f = drive.files().create(body=body, media_body=media, fields="id, webViewLink", supportsAllDrives=True).execute()
    if make_public:
        raise RuntimeError("Public Drive sharing is disabled for PUMA uploads.")
    return f["id"], f["webViewLink"]

# ---------- Canonicalization ----------
DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212"), ord('-'))
def canon(s: str) -> str:
    s = (s or "").translate(DASHES)
    s = re.sub(r'\s+', '', s).upper()
    return s
ALNUM = re.compile(r'[^A-Z0-9]')
def canon_strict(s: str) -> str:
    return ALNUM.sub('', (s or '').upper())

def canon_map(part_to_rows: Dict[str, List[int]]) -> Dict[str, List[int]]:
    out: Dict[str, List[int]] = {}
    for pn, rows in part_to_rows.items():
        out.setdefault(canon(pn), []).extend(rows)
        out.setdefault(canon_strict(pn), []).extend(rows)
    return out

# ---------- PDF parsing ----------
LINE_RX = re.compile(r'^\s*(?P<pn>.+?)\s*(?:\((?P<desc>.*?)\))?\s+(?P<qty>\d{1,6})\s+unit(?:s)?\b', re.I)
QTY_LINE = re.compile(r'\b(?P<qty>\d{1,6})\s+unit(?:s)?\b', re.I)
SKIP_HEAD_RX = re.compile(r'^(total\b|item\s*name\b|inventory\s*summary\b)$', re.I)

def parse_rr_pdf_summary(b: bytes) -> List[Tuple[int, str]]:
    """Supports:
       1) One-line: 'PN (desc) N unit(s)'
       2) Two-line: 'PN (desc ...,'  followed by 'N unit(s)' on the next line
    """
    items: List[Tuple[int, str]] = []
    with pdfplumber.open(io.BytesIO(b)) as pdf:
        if not pdf.pages: return items
        page = pdf.pages[0]
        raw = page.extract_text(x_tolerance=1.6, y_tolerance=3.2) or ""
        if DEBUG:
            print("[RR] Summary preview:")
            print("\n".join(raw.splitlines()[:40]))
        prev = None
        for ln in (ln.strip() for ln in raw.splitlines()):
            if not ln: continue
            if SKIP_HEAD_RX.search(ln): 
                prev = ln
                continue

            # Prefer one-line match
            m1 = LINE_RX.search(ln)
            if m1:
                qty = int(m1.group("qty"))
                pn_full = (m1.group("pn") or "").strip()
                pn = pn_full.split(" (", 1)[0].strip()
                if pn:
                    items.append((qty, pn))
                    if DEBUG: print(f"[RR] (+) {pn}  x{qty}")
                prev = ln
                continue

            # Fallback: qty on this line; PN/desc was the previous non-empty line
            m2 = QTY_LINE.search(ln)
            if m2 and prev and not SKIP_HEAD_RX.search(prev):
                qty = int(m2.group("qty"))
                pn = prev.split(" (", 1)[0].strip()
                if pn:
                    items.append((qty, pn))
                    if DEBUG: print(f"[RR] (+) {pn}  x{qty}  (2-line)")
                prev = ln
                continue

            prev = ln

    # Merge duplicates
    merged: Dict[str, int] = {}
    for q, pn in items:
        merged[pn] = merged.get(pn, 0) + q
    return [(qty, pn) for pn, qty in merged.items()]

# ---------- Helpers ----------
def col_letter(idx0: int) -> str:
    """0-based index -> column letters (A, B, ..., AA, AB, ...)"""
    n = idx0 + 1
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s

# ---------- Apply to tracker ----------
def apply_rr_to_tracker(sheets, project_tab, subject, email_dt, file_link, items, values, idx, canon_to_rows):
    tab = project_tab
    ensure_unmatched_tab(sheets)
    pn_col = idx.get("Part Number")
    qty_col = idx.get("Quantity")
    status_col = idx.get("Status")
    rr_col = idx.get("RR Link")  # Column J

    to_update_rows = set()
    status_updates: List[Tuple[int, str]] = []
    rr_updates: List[Tuple[int, str]] = []
    unmatched_rows = []
    _ln = lambda x: (str(x or "")).strip().lower()
    ts_text = email_dt.strftime("%Y-%m-%d %H:%M")

    for qty, cpn in items:
        key1, key2 = canon(cpn), canon_strict(cpn)
        match_rows = canon_to_rows.get(key1) or canon_to_rows.get(key2) or []
        if DEBUG: print(f"[RR] Match keys for {cpn} -> {key1} / {key2}; rows={match_rows}")
        if not match_rows:
            unmatched_rows.append([datetime.datetime.now().isoformat(timespec="seconds"), tab, subject, str(qty), cpn, "Part not on tracker", file_link or ""])
            if DEBUG: print(f"[RR] UNMATCHED: {cpn} (qty {qty})")
            continue

        def row_qty_of(r):
            if qty_col is not None and qty_col < len(values[r]):
                try: return int(float(values[r][qty_col]))
                except: return 1
            return 1

        def status_of(r):
            return _ln(values[r][status_col]) if status_col is not None and status_col < len(values[r]) else ""

        remaining = qty
        advancing = [
            r for r in match_rows
            if can_advance_status(status_of(r), "Received") and
               normalize_status(status_of(r)) != "received"
        ]
        others = [r for r in match_rows if r not in advancing]
        rows_to_touch = []
        for r in advancing + others:
            if remaining <= 0:
                break
            remaining -= row_qty_of(r)
            rows_to_touch.append(r)

        for r in rows_to_touch:
            to_update_rows.add(r)
            # Status -> Received (unless already Delivered)
            if status_col is not None:
                cur = status_of(r)
                if can_advance_status(cur, "Received") and normalize_status(cur) != "received":
                    status_updates.append((r, "Received"))
                    # also reflect in local cache so later items see the change
                    while len(values[r]) <= status_col: values[r].append("")
                    values[r][status_col] = "Received"

            # Date Received (Col J) -> hyperlink
            if rr_col is not None and file_link:
                rr_updates.append((r, f'=HYPERLINK("{file_link}","{ts_text}")'))

    # Batch update ONLY the cells we touched (preserve Column H hyperlinks)
    data = []
    if status_updates:
        col = col_letter(status_col)
        for r, val in status_updates:
            data.append({"range": f"{tab}!{col}{2+r}", "values": [[val]]})
    if rr_updates:
        col = col_letter(rr_col)
        for r, val in rr_updates:
            data.append({"range": f"{tab}!{col}{2+r}", "values": [[val]]})

    if data:
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption":"USER_ENTERED","data":data}
        ).execute()
        if DEBUG: print(f"[RR] Updated {len(to_update_rows)} row(s).")
    else:
        if DEBUG: print("[RR] No status changes were needed.")

    if unmatched_rows:
        sheets.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID, range="RR Unmatched!A2:G",
            valueInputOption="USER_ENTERED", body={"values": unmatched_rows}).execute()
        if DEBUG: print(f"[RR] Logged {len(unmatched_rows)} unmatched item(s).")

# ---------- Main ----------
if __name__ == "__main__":
    gmail, sheets, drive = setup_services()
    if not gmail or not sheets or not drive: raise SystemExit(1)

    msgs = list_rr_messages(gmail)
    error_count = 0
    for m in msgs:
        try:
            subject, atts, email_dt = get_subject_attachments_timestamp(gmail, m["id"])

            # Parse evidence BEFORE choosing a project or writing/uploading anything.
            items: List[Tuple[int, str]] = []
            pdf_attachments = []
            for fname, data in atts:
                if not fname.lower().endswith(".pdf"):
                    continue
                parsed_items = parse_rr_pdf_summary(data)
                if parsed_items:
                    pdf_attachments.append((fname, data))
                    items.extend(parsed_items)

            if not items:
                if DEBUG:
                    print("[RR] Skipping email: PDF didn't yield any line items.")
                # Leave unread so a human can review/re-send.
                continue

            resolution = resolve_subject_to_existing_tracker(
                sheets,
                SPREADSHEET_ID,
                subject,
                ['Receiving Report', 'RR'],
                parts=[pn for _, pn in items],
            )

            if not resolution.confirmed:
                if DEBUG:
                    print(
                        f"[RR] PROJECT {resolution.status}: {subject} "
                        f"method={resolution.method} suggestions={resolution.suggestions}"
                    )
                # Critical safety behavior: no tracker creation, no status write,
                # no report upload, and leave the email unread for review.
                continue

            tab_title = resolution.tracker_title
            tab_title, idx, values, part_to_rows = get_tracker_snapshot(sheets, tab_title)
            canon_to_rows = canon_map(part_to_rows)

            proj_name = resolution.canonical_project
            project_folder_id = get_or_create_child_folder(drive, RR_BASE_FOLDER_ID, proj_name)

            file_link = None
            for fname, data in pdf_attachments:
                _, link = upload_pdf_to_drive(
                    drive,
                    project_folder_id,
                    f"{proj_name} - {fname}".replace("/", "-"),
                    data,
                    MAKE_LINK_PUBLIC,
                )
                file_link = file_link or link

            if DEBUG:
                print(f"[RR] {subject} -> {tab_title} :: {len(items)} detected item(s); Link={'yes' if file_link else 'no'}")
                for q, cpn in items: print(f"  - ({q}) {cpn}")

            apply_rr_to_tracker(sheets, tab_title, subject, email_dt, file_link, items, values, idx, canon_to_rows)
            mark_read(gmail, m["id"])
        except Exception as e:
            error_count += 1
            print(f"[RR] Error processing message {m.get('id')}: {e}")

    print(f"PUMA6_RR complete. errors={error_count}")
    if error_count:
        raise SystemExit(1)

