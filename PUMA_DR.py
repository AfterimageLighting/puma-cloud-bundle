# PUMA1_DR.py — Delivery Report -> Drive upload + Status="Delivered" + Date Delivered hyperlink
# Based on PUMA6_RR.py

import os, re, io, sys, base64, datetime
from typing import List, Tuple, Dict
from puma_project_resolver import resolve_subject_to_existing_tracker

SPREADSHEET_ID = os.getenv('PUMA_SPREADSHEET_ID', '1pwVlYSGVjyTCLt4GT7xU2TCnxfdJuxAbp_jU6Snisls').strip()

def resolve_label_id(gmail, label_name: str) -> str:
    """Return Gmail labelId for a visible label name."""
    labs = gmail.users().labels().list(userId="me").execute().get("labels", [])
    for lab in labs:
        if lab.get("name") == label_name:
            return lab["id"]
    raise ValueError(f"Label '{label_name}' not found.")

def list_dr_messages(gmail):
    """Return a list of Gmail message IDs for unread Delivery Reports under PUMA - DR."""
    messages = []
    request = gmail.users().messages().list(
        userId="me",
        labelIds=[resolve_label_id(gmail, os.getenv('PUMA_DR_LABEL_NAME', 'PUMA - DR'))],
        q="is:unread has:attachment",            # keep it unread + attachment
        maxResults=50
    )
    while request is not None:
        response = request.execute()
        messages.extend(response.get("messages", []))
        request = gmail.users().messages().list_next(previous_request=request, previous_response=response)
    return messages
DR_BASE_FOLDER_ID = "1b6W8VNs77TA-UC5j-8NryQxlAebXYSyI"
MAKE_LINK_PUBLIC = False
DEBUG = True
if "--nodebug" in sys.argv:
    DEBUG = False

# ---- deps ----
try:
    import pdfplumber
except Exception:
    os.system(f"{sys.executable} -m pip install pdfplumber >/dev/null 2>&1")
    import pdfplumber

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
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
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists('client_secrets.json'):
                print("Error: client_secrets.json not found."); return None, None, None
            flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as f: f.write(creds.to_json())
    try:
        return (
            build('gmail', 'v1', credentials=creds),
            build('sheets', 'v4', credentials=creds),
            build('drive', 'v3', credentials=creds),
        )
    except HttpError as e:
        print(f"Auth error: {e}"); return None, None, None

# ---------- Gmail ----------
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
DASH_RX = re.compile(r'[\u2010-\u2015]')
def _clean_project_tokens(name: str) -> str:
    name = re.sub(r'(?i)\b(project|proj\.?)\b', '', name)
    name = re.sub(r'\s+', ' ', name).strip(' -–:— ')
    return name.strip()

def project_from_subject(subject: str) -> str:
    s = DASH_RX.sub('-', subject or "").strip()
    m = re.search(r'^(.*?)\s*[-:]\s*(?:delivery\s*report|dr)\b', s, re.I)
    if m and m.group(1).strip(): return _clean_project_tokens(m.group(1))
    m = re.search(r'(?:delivery\s*report|dr)\b\s*[-:]\s*(.*)$', s, re.I)
    if m and m.group(1).strip(): return _clean_project_tokens(m.group(1))
    s = re.sub(r'(?i)\bdelivery\s*report\b', '', s)
    s = re.sub(r'(?i)\bdr\b', '', s)
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
        elif (("date" in t and ("deliver" in t or "delivered" in t)) or t in {"dr", "date delivered", "delivery date"} or "dr link" in t):
            idx["DR Link"] = i
        elif (("date" in t and "receive" in t) or t.startswith("receiving rep") or t.startswith("receiving report") or t == "rr" or "rr link" in t):
            idx["RR Link"] = i

    res = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{tab_title}'!A2:L"
    ).execute()
    values = res.get("values", [])
    pn_col = idx.get("Part Number")
    part_to_rows: Dict[str, List[int]] = {}
    for r, row in enumerate(values):
        pn = (str(row[pn_col]).strip() if pn_col is not None and pn_col < len(row) else "")
        if pn:
            part_to_rows.setdefault(pn, []).append(r)

    if DEBUG:
        print(f"[DR] Using confirmed tracker tab: {tab_title}")
        print(f"[DR] Tracker dictionary loaded: {len(part_to_rows)} PNs")
    return tab_title, idx, values, part_to_rows

def ensure_unmatched_tab(sheets):
    tab = "DR Unmatched"
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
        drive.permissions().create(fileId=f["id"], body={"role":"reader","type":"anyone"}, supportsAllDrives=True).execute()
        f = drive.files().get(fileId=f["id"], fields="id, webViewLink", supportsAllDrives=True).execute()
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
    """Same parser as RR: supports
       1) One-line: 'PN (desc) N unit(s)'
       2) Two-line: 'PN (desc ...' then next line 'N unit(s)'
    """
    items: List[Tuple[int, str]] = []
    with pdfplumber.open(io.BytesIO(b)) as pdf:
        if not pdf.pages: return items
        page = pdf.pages[0]
        raw = page.extract_text(x_tolerance=1.6, y_tolerance=3.2) or ""
        if DEBUG:
            print("[DR] Summary preview:")
            print("\n".join(raw.splitlines()[:40]))
        prev = None
        for ln in (ln.strip() for ln in raw.splitlines()):
            if not ln: continue
            if SKIP_HEAD_RX.search(ln):
                prev = ln
                continue
            m1 = LINE_RX.search(ln)
            if m1:
                qty = int(m1.group("qty"))
                pn_full = (m1.group("pn") or "").strip()
                pn = pn_full.split(" (", 1)[0].strip()
                if pn:
                    items.append((qty, pn))
                    if DEBUG: print(f"[DR] (+) {pn}  x{qty}")
                prev = ln
                continue
            m2 = QTY_LINE.search(ln)
            if m2 and prev and not SKIP_HEAD_RX.search(prev):
                qty = int(m2.group("qty"))
                pn = prev.split(" (", 1)[0].strip()
                if pn:
                    items.append((qty, pn))
                    if DEBUG: print(f"[DR] (+) {pn}  x{qty}  (2-line)")
                prev = ln
                continue
            prev = ln
    merged: Dict[str, int] = {}
    for q, pn in items:
        merged[pn] = merged.get(pn, 0) + q
    return [(qty, pn) for pn, qty in merged.items()]

# ---------- Helpers ----------
def col_letter(idx0: int) -> str:
    n = idx0 + 1
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
# ---------- Apply to tracker ----------
def apply_dr_to_tracker(sheets, project_tab, subject, email_dt, file_link, items, values, idx, canon_to_rows):
    tab = project_tab
    ensure_unmatched_tab(sheets)
    pn_col = idx.get("Part Number")
    qty_col = idx.get("Quantity")
    status_col = idx.get("Status")
    dr_col = idx.get("DR Link")

    to_update_rows = set()
    status_updates: List[Tuple[int, str]] = []
    dr_updates: List[Tuple[int, str]] = []
    unmatched_rows = []
    _ln = lambda x: (str(x or "")).strip().lower()
    ts_text = email_dt.strftime("%Y-%m-%d %H:%M")

    for qty, cpn in items:
        key1, key2 = canon(cpn), canon_strict(cpn)
        match_rows = canon_to_rows.get(key1) or canon_to_rows.get(key2) or []
        if DEBUG: print(f"[DR] Match keys for {cpn} -> {key1} / {key2}; rows={match_rows}")
        if not match_rows:
            unmatched_rows.append([datetime.datetime.now().isoformat(timespec="seconds"), tab, subject, str(qty), cpn, "Part not on tracker", file_link or ""])
            if DEBUG: print(f"[DR] UNMATCHED: {cpn} (qty {qty})")
            continue

        def row_qty_of(r):
            if qty_col is not None and qty_col < len(values[r]):
                try: return int(float(values[r][qty_col]))
                except: return 1
            return 1

        def status_of(r):
            return _ln(values[r][status_col]) if status_col is not None and status_col < len(values[r]) else ""

        remaining = qty
        pending = [r for r in match_rows if status_of(r) not in {"delivered"}]
        others  = [r for r in match_rows if r not in pending]
        rows_to_touch = []
        for r in pending + others:
            if remaining <= 0: break
            if status_of(r) in {"delivered"}:
                rows_to_touch.append(r)  # still drop the link
                continue
            remaining -= row_qty_of(r)
            rows_to_touch.append(r)

        for r in rows_to_touch:
            to_update_rows.add(r)
            # Status -> Delivered
            if status_col is not None:
                cur = status_of(r)
                if cur != "delivered":
                    status_updates.append((r, "Delivered"))
                    while len(values[r]) <= status_col: values[r].append("")
                    values[r][status_col] = "Delivered"

            # Date Delivered -> hyperlink
            if dr_col is not None and file_link:
                dr_updates.append((r, f'=HYPERLINK("{file_link}","{ts_text}")'))

    # Batch update ONLY the cells we touched (preserve Column H hyperlinks)
    data = []
    if status_updates:
        col = col_letter(status_col)
        for r, val in status_updates:
            data.append({"range": f"{tab}!{col}{2+r}", "values": [[val]]})
    if dr_updates:
        col = col_letter(dr_col)
        for r, val in dr_updates:
            data.append({"range": f"{tab}!{col}{2+r}", "values": [[val]]})

    if data:
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption":"USER_ENTERED","data":data}
        ).execute()
        if DEBUG: print(f"[DR] Updated {len(to_update_rows)} row(s).")
    else:
        if DEBUG: print("[DR] No status changes were needed.")

    if unmatched_rows:
        sheets.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID, range="DR Unmatched!A2:G",
            valueInputOption="USER_ENTERED", body={"values": unmatched_rows}).execute()
        if DEBUG: print(f"[DR] Logged {len(unmatched_rows)} unmatched item(s).")

# ---------- Main ----------
if __name__ == "__main__":
    gmail, sheets, drive = setup_services()
    if not gmail or not sheets or not drive: raise SystemExit(1)

    msgs = list_dr_messages(gmail)
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
                    print("[DR] Skipping email: PDF didn't yield any line items.")
                # Leave unread so a human can review/re-send.
                continue

            resolution = resolve_subject_to_existing_tracker(
                sheets,
                SPREADSHEET_ID,
                subject,
                ['Delivery Report', 'DR'],
                parts=[pn for _, pn in items],
            )

            if not resolution.confirmed:
                if DEBUG:
                    print(
                        f"[DR] PROJECT {resolution.status}: {subject} "
                        f"method={resolution.method} suggestions={resolution.suggestions}"
                    )
                # Critical safety behavior: no tracker creation, no status write,
                # no report upload, and leave the email unread for review.
                continue

            tab_title = resolution.tracker_title
            tab_title, idx, values, part_to_rows = get_tracker_snapshot(sheets, tab_title)
            canon_to_rows = canon_map(part_to_rows)

            proj_name = resolution.canonical_project
            project_folder_id = get_or_create_child_folder(drive, DR_BASE_FOLDER_ID, proj_name)

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
                print(f"[DR] {subject} -> {tab_title} :: {len(items)} detected item(s); Link={'yes' if file_link else 'no'}")
                for q, cpn in items: print(f"  - ({q}) {cpn}")

            apply_dr_to_tracker(sheets, tab_title, subject, email_dt, file_link, items, values, idx, canon_to_rows)
            mark_read(gmail, m["id"])
        except Exception as e:
            print(f"[DR] Error processing message {m.get('id')}: {e}")

    print("PUMA1_DR complete.")

