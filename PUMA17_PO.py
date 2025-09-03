# PUMA17_PO.py — PO emails (QuickBooks) -> Ordered + Drive upload + hyperlink
# Changes vs PUMA16:
#   - QTY: pick the integer immediately BEFORE the first price in the 3-line window,
#          and ignore digits that belong to the PN
#   - De-dup: after a window match, advance by 3 lines to avoid overlapping re-detections

import os, re, io, sys, base64, datetime, difflib
from typing import List, Tuple, Dict

SPREADSHEET_ID = '1pwVlYSGVjyTCLt4GT7xU2TCnxfdJuxAbp_jU6Snisls'
GMAIL_QUERY = 'label:PUMA is:unread subject:"PO -" has:attachment'
PO_BASE_FOLDER_ID = "1VlCypDA_iF5dEUmA9c3E7ABYyS4-m6W2"
MAKE_LINK_PUBLIC = False
DEBUG = True
import sys
if "--debug" in sys.argv:
    DEBUG = True

# ---- deps ----
try:
    import pdfplumber
except ImportError:
    os.system(f"{sys.executable} -m pip install pdfplumber")
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
def list_po_messages(gmail):
    msgs, req = [], gmail.users().messages().list(userId="me", q=GMAIL_QUERY, maxResults=50)
    while req is not None:
        resp = req.execute(); msgs += resp.get("messages", []); req = gmail.users().messages().list_next(req, resp)
    if DEBUG: print(f"[PO] Found {len(msgs)} email(s).")
    return msgs

def get_subject_and_attachments(gmail, msg_id):
    msg = gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()
    subject = next((h["value"] for h in msg["payload"].get("headers", []) if h["name"].lower()=="subject"), "")
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
    return subject, atts

def mark_read(gmail, msg_id):
    gmail.users().messages().modify(userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}).execute()

# ---------- Subject → Project ----------
def project_from_subject(subject: str) -> str:
    s = subject.strip()
    parts = re.split(r'\s[-–]\s', s)
    guess = parts[-1].strip()
    guess = re.sub(r'^\d+\s[-–]\s', '', guess).strip()
    guess = re.sub(r'^PO[-_ ]?\d+\s[-–]\s', '', guess, flags=re.I).strip()
    return guess or s

# ---------- Sheets ----------
def _norm(x): return (str(x or "")).strip()
def _lnorm(x): return _norm(x).lower()

def _header_index_map(headers):
    idx = {}
    for i, h in enumerate(headers):
        t = _lnorm(h)
        if t in {"type","item type","category"}: idx["Type"] = i
        elif t in {"manufacturer","mfr","mfg","brand","vendor"}: idx["Manufacturer"] = i
        elif t in {"part number","part #","part no","item #","sku","item","item no"}: idx["Part Number"] = i
        elif t in {"qty","quantity","qnty","qty ordered","qty/ea"}: idx["Quantity"] = i
        elif t == "status": idx["Status"] = i
        elif "po number" in t or t == "po #": idx["PO Number"] = i
        elif "estimated ship" in t or "esd" in t or "ship date" in t: idx["ESD"] = i
    return idx

def _fetch_sheets_meta(sheets):
    return sheets.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute().get("sheets", [])

def _existing_project_tabs(sheets):
    tabs = []
    for sh in _fetch_sheets_meta(sheets):
        title = sh["properties"]["title"]
        if title.endswith(" - Project Tracker"):
            tabs.append(title)
    return tabs

def _best_project_title(sheets, guess_project: str) -> str:
    import difflib
    candidates = _existing_project_tabs(sheets)
    if not candidates:
        return f"{guess_project} - Project Tracker"
    def base(t): return t[:-len(" - Project Tracker")].strip().lower()
    guess_base = guess_project.strip().lower()
    scores = [(difflib.SequenceMatcher(None, base(t), guess_base).ratio(), t) for t in candidates]
    best_score, best_title = max(scores, key=lambda x: x[0])
    return best_title if best_score >= 0.70 else f"{guess_project} - Project Tracker"

def ensure_tracker_tab(sheets, project_guess):
    target_title = _best_project_title(sheets, project_guess)
    meta = _fetch_sheets_meta(sheets)
    titles = {s['properties']['title']: s['properties']['sheetId'] for s in meta}
    if target_title not in titles:
        sheets.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID,
            body={'requests':[{'addSheet':{'properties':{'title':target_title}}}]}).execute()
    headers = [["Project","Source","Type","Part Number","Manufacturer","Quantity","Status","PO Number","Estimated Ship Date (ESD)"]]
    sheets.spreadsheets().values().update(spreadsheetId=SPREADSHEET_ID, range=f"{target_title}!A1:I1",
        valueInputOption='RAW', body={'values': headers}).execute()
    hdr = sheets.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=f"{target_title}!1:1").execute().get("values", [[]])[0]
    return target_title, _header_index_map(hdr)

def get_tracker_snapshot(sheets, project_guess):
    tab, idx = ensure_tracker_tab(sheets, project_guess)
    res = sheets.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=f"{tab}!A2:I").execute()
    values = res.get("values", [])
    pn_col = idx.get("Part Number")
    part_to_rows: Dict[str, List[int]] = {}
    for r, row in enumerate(values):
        pn = _norm(row[pn_col]) if pn_col is not None and pn_col < len(row) else ""
        if pn: part_to_rows.setdefault(pn, []).append(r)
    return tab, idx, values, part_to_rows

def ensure_unmatched_tab(sheets):
    tab = "PO Unmatched"
    meta = _fetch_sheets_meta(sheets)
    titles = {s['properties']['title'] for s in meta}
    if tab not in titles:
        sheets.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID,
            body={'requests':[{'addSheet':{'properties':{'title':tab}}}]}).execute()
        sheets.spreadsheets().values().update(spreadsheetId=SPREADSHEET_ID, range=f"{tab}!A1:G1",
            valueInputOption='RAW', body={'values': [["Timestamp","Project","Email Subject","Qty","Part Number","PO Number","Note"]]}).execute()
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

def canon_map(part_to_rows: Dict[str, List[int]]) -> Dict[str, List[int]]:
    out = {}
    for pn, rows in part_to_rows.items():
        out.setdefault(canon(pn), []).extend(rows)
    return out

# allow PN matching with junk between tokens (e.g., "S", "S-Driver", prices) between pieces
_CPN_RX_CACHE = {}
def pn_regex_for(cpn: str):
    key = canon(cpn)
    pat = _CPN_RX_CACHE.get(key)
    if pat: return pat
    tokens = re.findall(r'[A-Z0-9]+', key)   # split PN into alnum chunks
    rx = re.compile(".*?".join(map(re.escape, tokens)), re.I)
    _CPN_RX_CACHE[key] = rx
    return rx

# ---------- PDF parsing ----------
INT_ANY = re.compile(r'\b(\d{1,6})\b')
PRICE_RX = re.compile(r'\b\d{1,3}(?:,\d{3})*\.\d{2}\b')  # e.g., 94.33 or 1,320.62

def _strip_parens(s: str) -> str: return re.sub(r'\(.*?\)', ' ', s)

def parse_po_pdf_with_tracker(b: bytes, cpn_list: List[str]) -> Dict[str, object]:
    items, po_number = [], None
    with pdfplumber.open(io.BytesIO(b)) as pdf:
        for page in pdf.pages:
            raw = page.extract_text(x_tolerance=1.6, y_tolerance=3.2) or ""
            if not raw:
                continue

            if not po_number:
                m = re.search(r'\b(?:PO|P\.O\.|Purchase\s*Order)\s*(?:No\.?|#|Number)\s*[:\-]?\s*([A-Z0-9\-\/]+)', raw, re.I)
                if m: po_number = m.group(1).strip()

            lines = [ln.rstrip() for ln in raw.splitlines()]

            if DEBUG:
                for j, ln in enumerate(lines[:30]):
                    print(f"[TXT {j:02d}] {ln}")

            i = 0
            while i < len(lines):
                # 3-line window for wraps/quirks
                chunk_orig = " ".join(lines[i:i+3])
                chunk_no_paren = " ".join(part.split('(')[0] for part in lines[i:i+3])
                win_flat = re.sub(r'[^A-Za-z0-9]', '', (chunk_no_paren or chunk_orig)).upper()

                # PN hit allowing junk between tokens
                hits = [cpn for cpn in cpn_list if pn_regex_for(cpn).search(win_flat)]
                if not hits:
                    i += 1
                    continue

                hits.sort(key=len, reverse=True)
                hit = hits[0]
                if DEBUG: print(f"[PO] Detected PN window hit: {hit}")

                # --- QTY: integer immediately BEFORE the first price in the window ---
                qty = None
                price_m = PRICE_RX.search(chunk_orig)

                # remove PN digits from the search string (so '2' in C-2 etc. don't look like qty)
                search_str = chunk_orig
                for d in re.findall(r'\d+', hit):
                    search_str = re.sub(rf'\b{re.escape(d)}\b', ' ', search_str)

                cands = [(m.start(), int(m.group(0))) for m in re.finditer(r'\b\d{1,4}\b', search_str)]
                if price_m and cands:
                    before = [(pos, val) for (pos, val) in cands if pos < price_m.start()]
                    if before:
                        qty = before[-1][1]  # last integer before price

                if qty is None:
                    # fallback: first reasonable integer in/near the block
                    for _, val in cands:
                        if 1 <= val <= 1000:
                            qty = val
                            break

                if qty is None:
                    # last fallback: look after ')' a few lines below
                    k = i
                    while k < len(lines) and ')' not in lines[k]:
                        k += 1
                    for t in range(k+1, min(k+8, len(lines))):
                        ln = _strip_parens(lines[t])
                        mqty = INT_ANY.search(ln)
                        if mqty:
                            try:
                                qv = int(mqty.group(1))
                                if 0 < qv < 100000:
                                    qty = qv
                                    break
                            except:
                                pass

                if qty is not None:
                    items.append((qty, hit))
                    if DEBUG: print(f"[PO]   -> qty {qty}")
                    i += 3  # skip overlapping window to avoid duplicate detections
                else:
                    i += 1

    # Merge duplicates (extra safety)
    merged: Dict[str, int] = {}
    for q, cpn in items:
        merged[cpn] = merged.get(cpn, 0) + q

    return {"po_number": po_number, "items": [(q, cpn) for cpn, q in merged.items()]}

# ---------- Apply to tracker ----------
def apply_to_tracker(sheets, project_tab, subject, po_number, file_link, items, values, idx, canon_to_rows):
    tab = project_tab
    ensure_unmatched_tab(sheets)
    pn_col, qty_col, status_col, po_col = idx.get("Part Number"), idx.get("Quantity"), idx.get("Status"), idx.get("PO Number")

    def set_cell(row, cidx, val):
        if cidx is None: return row
        while len(row) <= cidx: row.append("")
        row[cidx] = val; return row

    to_update, unmatched_rows = {}, []
    def _ln(x): return (str(x or "")).strip().lower()

    for po_qty, cpn in items:
        match_rows = canon_to_rows.get(canon(cpn), []) or canon_to_rows.get(cpn, [])
        if not match_rows:
            unmatched_rows.append([datetime.datetime.now().isoformat(timespec="seconds"), tab, subject, str(po_qty), cpn, (po_number or ""), "Part not on tracker"])
            if DEBUG: print(f"[PO] UNMATCHED: {cpn} (qty {po_qty})")
            continue

        def row_qty_of(r):
            if qty_col is not None and qty_col < len(values[r]):
                try: return int(float(values[r][qty_col]))
                except: return 1
            return 1

        status_of = lambda r: (_ln(values[r][status_col]) if status_col < len(values[r]) else "")
        remaining = po_qty
        pending = [r for r in match_rows if status_of(r) not in {"ordered","received","delivered"}]
        others  = [r for r in match_rows if r not in pending]
        rows_to_touch = []
        for r in pending + others:
            if remaining <= 0: break
            if status_of(r) in {"received","delivered"}:
                continue
            remaining -= row_qty_of(r)
            rows_to_touch.append(r)

        used = po_qty - max(remaining, 0)
        if DEBUG: print(f"[PO] Allocated {used}/{po_qty} for {cpn} across {len(rows_to_touch)} row(s).")

        for r in rows_to_touch:
            row = list(values[r]) if r < len(values) else []
            while len(row) <= status_col: row.append("")
            cur = _ln(row[status_col]) if status_col < len(row) else ""
            if cur in {"received","delivered"}:
                if po_col is not None and file_link:
                    row = set_cell(row, po_col, f'=HYPERLINK("{file_link}","{po_number or "View PO"}")')
                    to_update[r] = row
                continue
            row[status_col] = "Ordered"
            if po_col is not None:
                if file_link:
                    row = set_cell(row, po_col, f'=HYPERLINK("{file_link}","{po_number or "View PO"}")')
                elif po_number:
                    row = set_cell(row, po_col, po_number)
            to_update[r] = row

    if to_update:
        data = [{"range": f"{tab}!A{2+r}:I{2+r}", "values": [row[:9] + ([''] * max(0, 9-len(row)))]}
                for r, row in sorted(to_update.items())]
        sheets.spreadsheets().values().batchUpdate(spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption":"USER_ENTERED","data":data}).execute()
        if DEBUG: print(f"[PO] Updated {len(to_update)} row(s).")
    else:
        if DEBUG: print("[PO] No status changes were needed.")

    if unmatched_rows:
        sheets.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID, range="PO Unmatched!A2:G",
            valueInputOption="USER_ENTERED", body={"values": unmatched_rows}).execute()
        if DEBUG: print(f"[PO] Logged {len(unmatched_rows)} unmatched item(s).")

# ---------- Main ----------
if __name__ == "__main__":
    gmail, sheets, drive = setup_services()
    if not gmail or not sheets or not drive: raise SystemExit(1)

    msgs = list_po_messages(gmail)
    for m in msgs:
        try:
            subject, atts = get_subject_and_attachments(gmail, m["id"])
            guess_project = project_from_subject(subject)

            # Fuzzy resolve correct tab
            tab_title, idx, values, part_to_rows = get_tracker_snapshot(sheets, guess_project)
            canon_to_rows = canon_map(part_to_rows)
            cpn_list = sorted({c for c in canon_to_rows.keys()}, key=len, reverse=True)

            if DEBUG:
                print(f"[PO] Tracker dictionary loaded: {len(cpn_list)} PNs")
                if cpn_list:
                    print("[PO] Sample PNs:", ", ".join(cpn_list[:3]))

            proj_name = tab_title[:-len(" - Project Tracker")] if tab_title.endswith(" - Project Tracker") else tab_title
            project_folder_id = get_or_create_child_folder(drive, PO_BASE_FOLDER_ID, proj_name)

            po_number, items, file_link = None, [], None
            for fname, data in atts:
                if not fname.lower().endswith(".pdf"): continue
                parsed = parse_po_pdf_with_tracker(data, cpn_list)
                if not po_number and parsed.get("po_number"): po_number = parsed["po_number"]

                drive_name = f"{proj_name} - {po_number or fname}".replace("/", "-")
                _, link = upload_pdf_to_drive(drive, project_folder_id, drive_name, data, MAKE_LINK_PUBLIC)
                file_link = file_link or link

                items.extend(parsed.get("items", []))
                if not po_number:
                    mnum = re.search(r'\bPO[-_ ]?[A-Za-z0-9\-]+', fname, re.I)
                    if mnum: po_number = mnum.group(0)

            if DEBUG:
                print(f"[PO] {subject} -> {tab_title} :: {len(items)} detected item(s); PO={po_number or '(none)'}; Link={'yes' if file_link else 'no'}")
                for q, cpn in items: print(f"  - ({q}) {cpn}")

            apply_to_tracker(sheets, tab_title, subject, po_number, file_link, items, values, idx, canon_to_rows)
            mark_read(gmail, m["id"])
        except Exception as e:
            print(f"[PO] Error processing message {m.get('id')}: {e}")

    print("PUMA17_PO complete.")

