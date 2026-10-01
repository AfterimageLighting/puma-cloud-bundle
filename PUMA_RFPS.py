
# PUMA5_RFPS.py — RFPS emails (email body) -> Scheduled + Date Scheduled hyperlink
# Notes:
#  - Captures Gmail timestamp + link and writes to column K ("Date Scheduled") as a HYPERLINK()
#  - Correctly marks processed emails as READ (requires gmail.modify scope; reauthorize after deleting token.json)
#  - Safe for existing tabs; only sets full headers when creating a new tracker tab

import os, re, io, sys, base64, datetime
from typing import List, Tuple, Dict
from puma_project_resolver import resolve_subject_to_existing_tracker
from puma_runtime_config import test_safe_env

SPREADSHEET_ID = test_safe_env('PUMA_SPREADSHEET_ID', '1pwVlYSGVjyTCLt4GT7xU2TCnxfdJuxAbp_jU6Snisls')

def list_dr_messages(gmail):
    """Return a list of Gmail message IDs for unread Requests for Packing Slips under PUMA - DR."""
    messages = []
    request = gmail.users().messages().list(
        userId="me",
        labelIds=[test_safe_env('PUMA_RFPS_LABEL_ID', 'Label_5687824868171199039')],
        q="is:unread",            # keep it unread
        maxResults=50
    )
    while request is not None:
        response = request.execute()
        messages.extend(response.get("messages", []))
        request = gmail.users().messages().list_next(previous_request=request, previous_response=response)
    return messages

DEBUG = ("--debug" in sys.argv)

# ---------- Google API deps ----------
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except Exception:
    os.system(f"{sys.executable} -m pip install --quiet google-api-python-client google-auth-httplib2 google-auth-oauthlib")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/spreadsheets',
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
                print("Error: client_secrets.json not found."); return None, None
            flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json','w') as f: f.write(creds.to_json())
    try:
        gmail  = build('gmail',  'v1', credentials=creds)
        sheets = build('sheets', 'v4', credentials=creds)
        return gmail, sheets
    except HttpError as e:
        print(f"Auth error: {e}"); return None, None

# ---------- Helpers ----------
def _norm(x): return (str(x or "")).strip()
def _lnorm(x): return _norm(x).lower()
DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212"), ord('-'))
def canon(s: str) -> str:
    s = (s or "").translate(DASHES)
    s = re.sub(r'\s+', '', s).upper()
    return s

def _col_letter(idx_zero_based: int) -> str:
    """0 -> A, 1 -> B, ..."""
    idx = idx_zero_based
    letters = ""
    while True:
        idx, rem = divmod(idx, 26)
        letters = chr(ord('A') + rem) + letters
        if idx == 0:
            break
        idx -= 1
    return letters

# ---------- Gmail ----------
def list_rfps_messages(gmail):
    """Return unread RFPS emails under the PUMA - RFPS label."""
    msgs = []
    req = gmail.users().messages().list(
        userId="me",
        labelIds=[test_safe_env('PUMA_RFPS_LABEL_ID', 'Label_5687824868171199039')],
        q="is:unread",                     # RFPS parses from body, so attachments aren't required
        maxResults=100
    )
    while req is not None:
        resp = req.execute()
        msgs.extend(resp.get("messages", []))
        req = gmail.users().messages().list_next(previous_request=req, previous_response=resp)
    if DEBUG: print(f"[RFPS] Found {len(msgs)} email(s).")
    return msgs

def _walk_parts(payload):
    if "parts" in payload:
        for p in payload["parts"]:
            yield from _walk_parts(p)
    else:
        yield payload

def get_subject_and_body(gmail, msg_id):
    msg = gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()
    subject = next((h["value"] for h in msg["payload"].get("headers", []) if h["name"].lower()=="subject"), "")
    # Gmail internalDate is ms since epoch, as a string
    internal_ms = int(msg.get("internalDate", "0"))
    ts = datetime.datetime.fromtimestamp(internal_ms/1000.0)
    email_link = f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"

    text = ""
    for p in _walk_parts(msg["payload"]):
        mime = p.get("mimeType","").lower()
        b64  = p.get("body",{}).get("data")
        if not b64: continue
        data = base64.urlsafe_b64decode(b64).decode(errors="ignore")
        if "text/plain" in mime:
            text += "\n" + data
        elif "text/html" in mime:
            tmp = re.sub(r'(?i)<br\s*/?>', '\n', data)
            tmp = re.sub(r'(?s)<style.*?>.*?</style>', '', tmp, flags=re.I)
            tmp = re.sub(r'(?s)<script.*?>.*?</script>', '', tmp, flags=re.I)
            tmp = re.sub(r'<[^>]+>', '', tmp)
            text += "\n" + tmp
    return subject.strip(), text.strip(), ts, email_link

def mark_read(gmail, msg_id):
    # Requires gmail.modify scope; delete token.json and reauthorize if it seems ignored
    gmail.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"removeLabelIds": ["UNREAD"]}
    ).execute()

# ---------- Sheets ----------
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
        elif "date scheduled" in t: idx["Date Scheduled"] = i
        elif "date received" in t: idx["Date Received"] = i
        elif "date delivered" in t: idx["Date Delivered"] = i
    return idx

def _fetch_sheets_meta(sheets):
    return sheets.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute().get("sheets", [])

def get_tracker_snapshot(sheets, tab_title):
    """Load a CONFIRMED existing tracker. Never creates or fuzzy-selects a tab."""
    titles = {s["properties"]["title"] for s in _fetch_sheets_meta(sheets)}
    if tab_title not in titles:
        raise RuntimeError(f"Confirmed tracker no longer exists: {tab_title}")

    hdr = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab_title}'!1:1"
    ).execute().get("values", [[]])[0]
    idx = _header_index_map(hdr)

    res = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab_title}'!A2:L"
    ).execute()
    values = res.get("values", [])
    pn_col = idx.get("Part Number")
    part_to_rows: Dict[str, List[int]] = {}
    for r, row in enumerate(values):
        pn = _norm(row[pn_col]) if pn_col is not None and pn_col < len(row) else ""
        if pn:
            part_to_rows.setdefault(pn, []).append(r)
    return tab_title, idx, values, part_to_rows

def ensure_unmatched_tab(sheets):
    tab = "RFPS Unmatched"
    titles = {s['properties']['title'] for s in _fetch_sheets_meta(sheets)}
    if tab not in titles:
        sheets.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID,
            body={'requests':[{'addSheet':{'properties':{'title':tab}}}]}).execute()
        sheets.spreadsheets().values().update(spreadsheetId=SPREADSHEET_ID, range=f"{tab}!A1:F1",
            valueInputOption='RAW', body={'values': [["Timestamp","Project","Email Subject","Qty","Part Number","Note"]]}).execute()
    return tab

# ---------- Subject → Project ----------
def project_from_subject(subject: str) -> str:
    s = subject.strip()
    parts = re.split(r'\s[-–]\s', s)
    guess = parts[-1].strip()
    guess = re.sub(r'^\d+\s[-–]\s', '', guess).strip()
    guess = re.sub(r'^RFPS[-_ ]?\d*\s[-–]\s', '', guess, flags=re.I).strip()
    return guess or s

# ---------- Parsing RFPS body ----------
PHONE_RX = re.compile(r'\(?\d{3}\)?[ -.]?\d{3}[ -.]?\d{4}')

def _strip_signature(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines()]
    out = []
    for ln in lines:
        if ln.strip() in {"--", "—"}: break
        if PHONE_RX.search(ln) and len(out) > 4: break
        if re.search(r'^\s*(thanks|thank you|best|regards|sincerely)\b', ln, re.I) and len(out) > 4: break
        if re.search(r'afterimage\s+lighting', ln, re.I): break
        out.append(ln)
    return "\n".join(out)

def parse_items(body: str) -> List[Tuple[int,str]]:
    """
    Lines supported:
      (17) LT560WH6930R
      17x E7ICAT
      17 E7ICAT
    """
    body = _strip_signature(body)
    items = []
    for ln in body.splitlines():
        ln = ln.strip()
        if not ln: continue
        m = re.search(r'\(\s*(\d{1,5})\s*\)\s*([A-Z0-9][A-Z0-9\-]+)', ln)
        if not m:
            m = re.search(r'\b(\d{1,5})\s*[xX]\s+([A-Z0-9][A-Z0-9\-]+)', ln)
        if not m:
            m = re.search(r'^\s*(\d{1,5})\s+([A-Z0-9][A-Z0-9\-]+)', ln)
        if m:
            qty = int(m.group(1)); pn = m.group(2).upper()
            items.append((qty, pn))
    return items

# ---------- Apply to tracker ----------
def apply_to_tracker(sheets, project_tab, subject, items, values, idx, canon_to_rows, ts, email_link):
    tab = project_tab
    ensure_unmatched_tab(sheets)

    pn_col = idx.get("Part Number")
    qty_col = idx.get("Quantity")
    status_col = idx.get("Status")
    date_scheduled_col = idx.get("Date Scheduled")

    def set_cell(row, cidx, val):
        if cidx is None: return row
        while len(row) <= cidx: row.append("")
        row[cidx] = val; return row

    to_update, unmatched_rows = {}, []
    def status_of(r): 
        return _lnorm(values[r][status_col]) if (status_col is not None and status_col < len(values[r])) else ""
    def row_qty_of(r):
        if qty_col is not None and qty_col < len(values[r]):
            try: return int(float(values[r][qty_col]))
            except: return 1
        return 1

    for req_qty, pn in items:
        cpn = canon(pn)
        match_rows = canon_to_rows.get(cpn, []) or canon_to_rows.get(pn, [])
        if not match_rows:
            unmatched_rows.append([datetime.datetime.now().isoformat(timespec="seconds"), tab, subject, str(req_qty), pn, "Part not on tracker"])
            if DEBUG: print(f"[RFPS] UNMATCHED: {pn} (qty {req_qty})")
            continue

        # Allocate across any row except Delivered
        eligible = [r for r in match_rows if status_of(r) != "delivered"]
        remaining = req_qty
        rows_to_touch = []
        for r in eligible:
            if remaining <= 0: break
            remaining -= row_qty_of(r)
            rows_to_touch.append(r)

        used = req_qty - max(remaining, 0)
        if DEBUG: print(f"[RFPS] Allocated {used}/{req_qty} for {pn} across {len(rows_to_touch)} row(s).")

        for r in rows_to_touch:
            row = list(values[r]) if r < len(values) else []
            cur = status_of(r)
            if cur == "delivered": 
                continue
            set_cell(row, status_col, "Scheduled")
            # Insert hyperlink timestamp into Date Scheduled
            if date_scheduled_col is not None:
                link_formula = f'=HYPERLINK("{email_link}", "{ts.strftime("%Y-%m-%d %H:%M")}")'
                set_cell(row, date_scheduled_col, link_formula)
            to_update[r] = row

    if to_update:
        # Determine how far we need to write (at least through I, maybe farther if Date Scheduled exists)
        max_col = 8  # I is 0-based 8
        if date_scheduled_col is not None:
            max_col = max(max_col, date_scheduled_col)
        end_col_letter = _col_letter(max_col)
        data = []
        for r, row in sorted(to_update.items()):
            # pad to end col
            need_len = max(len(row), max_col+1)
            if len(row) < need_len:
                row = row + ([''] * (need_len - len(row)))
            rng = f"{tab}!A{2+r}:{end_col_letter}{2+r}"
            data.append({"range": rng, "values": [row[:need_len]]})

        sheets.spreadsheets().values().batchUpdate(spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption":"USER_ENTERED","data":data}).execute()
        if DEBUG: print(f"[RFPS] Updated {len(to_update)} row(s).")
    else:
        if DEBUG: print("[RFPS] No status changes were needed.")

    if unmatched_rows:
        sheets.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID, range="RFPS Unmatched!A2:F",
            valueInputOption="USER_ENTERED", body={"values": unmatched_rows}).execute()
        if DEBUG: print(f"[RFPS] Logged {len(unmatched_rows)} unmatched item(s).")

# ---------- Main ----------
if __name__ == "__main__":
    gmail, sheets = setup_services()
    if not gmail or not sheets: raise SystemExit(1)

    msgs = list_rfps_messages(gmail)
    error_count = 0
    for m in msgs:
        try:
            subject, body, ts, email_link = get_subject_and_body(gmail, m["id"])

            # Parse the requested items before choosing a tracker. Exact item
            # evidence may confirm a misspelled/short project name.
            items = parse_items(body)
            if not items:
                if DEBUG:
                    print(f"[RFPS] REVIEW: no parsable line items in {subject}")
                # Leave unread for manual review.
                continue

            resolution = resolve_subject_to_existing_tracker(
                sheets,
                SPREADSHEET_ID,
                subject,
                ["RFPS", "Request for Packing Slip", "Request for Packing Slips"],
                parts=[pn for _, pn in items],
            )

            if not resolution.confirmed:
                if DEBUG:
                    print(
                        f"[RFPS] PROJECT {resolution.status}: {subject} "
                        f"method={resolution.method} suggestions={resolution.suggestions}"
                    )
                # No tracker creation and no status change when project identity
                # is not confirmed. Leave unread for review.
                continue

            tab_title = resolution.tracker_title
            tab_title, idx, values, part_to_rows = get_tracker_snapshot(sheets, tab_title)

            # Canon map for fast PN lookups
            canon_to_rows: Dict[str, List[int]] = {}
            for pn, rows in part_to_rows.items():
                canon_to_rows.setdefault(canon(pn), []).extend(rows)

            if DEBUG:
                print(f"[RFPS] {subject} -> {tab_title} :: parsed {len(items)} item(s)")
                for q, pn in items: print(f"  - ({q}) {pn}")

            apply_to_tracker(sheets, tab_title, subject, items, values, idx, canon_to_rows, ts, email_link)
            mark_read(gmail, m["id"])
        except Exception as e:
            error_count += 1
            print(f"[RFPS] Error processing message {m.get('id')}: {e}")

    print(f"PUMA5_RFPS complete. errors={error_count}")
    if error_count:
        raise SystemExit(1)
