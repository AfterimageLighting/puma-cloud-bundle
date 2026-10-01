# PUMA4_RFPO.py — RFPO parser (email body + PDF attachments) with signature cut-off + allocator mode + table-gated PDF parsing
import os, re, base64, datetime, html, io, sys
from typing import List, Tuple, Dict, Optional
from puma_project_resolver import resolve_subject_to_existing_tracker
from puma_runtime_config import test_safe_env

# ============================ Config ============================
SPREADSHEET_ID = test_safe_env('PUMA_SPREADSHEET_ID', '1pwVlYSGVjyTCLt4GT7xU2TCnxfdJuxAbp_jU6Snisls')
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/spreadsheets'
]
DEBUG = True

# Behavior

def list_dr_messages(gmail):
    """Return a list of Gmail message IDs for unread Requests for Purchase Orders under PUMA - DR."""
    messages = []
    request = gmail.users().messages().list(
        userId="me",
        labelIds=[test_safe_env('PUMA_RFPO_LABEL_ID', 'Label_1550910640571640980')],
        q="is:unread",            # keep it unread
        maxResults=50
    )
    while request is not None:
        response = request.execute()
        messages.extend(response.get("messages", []))
        request = gmail.users().messages().list_next(previous_request=request, previous_response=response)
    return messages

ALLOCATE_BY_QTY = True   # True = allocate qty across duplicate parts; False = approve all matches

# ============================ Deps ============================
try:
    import pdfplumber  # type: ignore
except ImportError:
    print("Installing pdfplumber...")
    os.system(f"{sys.executable} -m pip install pdfplumber")
    import pdfplumber  # type: ignore

# ============================ Google auth/services ============================
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

def setup_services():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists('client_secrets.json'):
                print("Error: 'client_secrets.json' not found.")
                return None, None
            flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    try:
        return build('gmail', 'v1', credentials=creds), build('sheets', 'v4', credentials=creds)
    except HttpError as e:
        print(f"Auth error: {e}")
        return None, None

# ============================ Helpers ============================
def _norm(s: Optional[str]) -> str:  return (str(s or "")).strip()
def _lnorm(s: Optional[str]) -> str: return _norm(s).lower()

def _header_index_map(headers: List[str]) -> Dict[str, int]:
    idx = {}
    for i, h in enumerate(headers):
        t = _lnorm(h)
        if t in {"type","item type","category"}: idx["Type"] = i
        elif t in {"manufacturer","mfr","mfg","brand","vendor"}: idx["Manufacturer"] = i
        elif t in {"part number","part #","part no","item #","sku","item","item no"}: idx["Part Number"] = i
        elif t in {"qty","quantity","qnty","qty ordered","qty/ea"}: idx["Quantity"] = i
        elif t == "status": idx["Status"] = i
    return idx

def _load_existing_tracker(sheets, tracker_tab: str) -> Tuple[str, Dict[str, int]]:
    """Load a CONFIRMED existing tracker. Never creates a project tab."""
    meta = sheets.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    titles = {s['properties']['title'] for s in meta.get('sheets', [])}
    if tracker_tab not in titles:
        raise RuntimeError(f"Confirmed tracker no longer exists: {tracker_tab}")

    hdr = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tracker_tab}'!1:1"
    ).execute().get("values", [[]])[0]
    return tracker_tab, _header_index_map(hdr)

def _ensure_unmatched_tab(sheets) -> str:
    tab = "RFPO Unmatched"
    meta = sheets.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    titles = {s['properties']['title']: s['properties']['sheetId'] for s in meta.get('sheets', [])}
    if tab not in titles:
        sheets.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={'requests':[{'addSheet':{'properties':{'title':tab}}}]}).execute()
        sheets.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID, range=f"{tab}!A1:F1", valueInputOption='RAW',
            body={'values': [["Timestamp","Project","Email Subject","Qty","Part Number","Note"]]}
        ).execute()
    return tab

# ============================ Gmail ============================
def list_rfpo_messages(gmail):
    """Return unread RFPO emails under the PUMA - RFPO label."""
    msgs = []
    req = gmail.users().messages().list(
        userId="me",
        labelIds=[test_safe_env('PUMA_RFPO_LABEL_ID', 'Label_1550910640571640980')],
        q="is:unread has:attachment",
        maxResults=100
    )
    while req is not None:
        resp = req.execute()
        msgs.extend(resp.get("messages", []))
        req = gmail.users().messages().list_next(previous_request=req, previous_response=resp)
    if DEBUG: print(f"[RFPO] Found {len(msgs)} email(s).")
    return msgs

def _get_subject_bodies_attachments(gmail, msg_id):
    msg = gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "")

    bodies: List[Tuple[str,str]] = []
    atts: List[Tuple[str, bytes]] = []

    def walk(part):
        if "parts" in part:
            for p in part["parts"]:
                yield from walk(p)
        else:
            yield part

    for p in walk(payload):
        mime = p.get("mimeType", "")
        body = p.get("body", {})
        data = body.get("data")
        if "attachmentId" in body and body["attachmentId"]:
            a = gmail.users().messages().attachments().get(userId="me", messageId=msg_id, id=body["attachmentId"]).execute()
            atts.append((p.get("filename") or "", base64.urlsafe_b64decode(a["data"])))
        elif data:
            bodies.append((mime, base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")))
    return subject, bodies, atts

# ============================ Parsers ============================
# Body lines like: "(17) LT560WH6930R"
RFPO_BODY_LINE = re.compile(r"^\s*\(?(\d{1,6})\)?\s*[-x]*\s*([A-Za-z0-9._/\-]+)\s*$")
PHONE_LINE = re.compile(r"""^\s*(\+?1[-\s\.]?)?\(?\d{3}\)?[-\s\.]?\d{3}[-\s\.]?\d{4}\s*$""")

def _strip_after_signature(text: str) -> str:
    lines = text.splitlines()
    out, seen_item = [], False
    for raw in lines:
        line = raw.rstrip()
        if seen_item:
            l = line.strip(); ll = l.lower()
            if l.startswith("--") or ll in {"thanks,", "thank you,", "thanks", "thank you"}: break
            if "gmail_signature" in ll: break
            if PHONE_LINE.match(l): break
            if any(tok in ll for tok in ["afterimagelighting.com", "best,", "regards,"]): break
        if RFPO_BODY_LINE.match(line):
            seen_item = True
        out.append(line)
    return "\n".join(out)

def parse_rfpo_items_from_bodies(parts: List[Tuple[str,str]]) -> List[Tuple[int, str]]:
    blobs = []
    for mime, content in parts:
        if mime.startswith("text/plain"):
            blobs.append(content)
        elif mime.startswith("text/html"):
            content = re.sub(r'(?is)<div[^>]+class=["\']gmail_signature["\'][^>]*>.*?</div>', '', content)
            clean = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", content)
            clean = re.sub(r"(?is)<br\s*/?>", "\n", clean)
            clean = re.sub(r"(?is)</p>", "\n", clean)
            clean = re.sub(r"(?is)<.*?>", " ", clean)
            clean = html.unescape(clean)
            blobs.append(clean)
    trimmed = _strip_after_signature("\n".join(blobs))
    out: List[Tuple[int,str]] = []
    for line in trimmed.splitlines():
        m = RFPO_BODY_LINE.match(line)
        if m:
            qty = int(m.group(1)); part = m.group(2).strip()
            if qty > 0: out.append((qty, part))
    return out

# -------- PDF parsing (table-gated) --------
PRICE_TOK = re.compile(r"\$\s*[\d,]+(?:\.\d{2})?")
PDF_QTY_PREFIX = re.compile(r"^\s*(\d{1,6})\s+(.*)$", re.IGNORECASE)

TABLE_START_HINTS = ("qty", "description", "unit price")
TABLE_END_HINTS = ("total", "prices firm", "statement", "page:")

SKIP_TOKENS = {"freight"}  # add as needed

def parse_rfpo_items_from_pdf_bytes(b: bytes) -> List[Tuple[int, str]]:
    items: List[Tuple[int,str]] = []
    with pdfplumber.open(io.BytesIO(b)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            in_table = False
            for raw in text.splitlines():
                line = raw.strip()
                if not line:
                    continue
                low = line.lower()

                # find table header
                if not in_table:
                    if ("qty" in low) and (("description" in low) or ("unit price" in low)):
                        in_table = True
                    continue

                # stop at end cues
                if any(h in low for h in TABLE_END_HINTS):
                    break

                # line must include a price token to be a real item row
                if not PRICE_TOK.search(line):
                    continue

                # parse qty at start
                m = PDF_QTY_PREFIX.match(line)
                if not m:
                    continue
                try:
                    qty = int(m.group(1))
                except:
                    continue
                if qty <= 0:
                    continue

                rest = m.group(2)
                if any(tok in rest.lower() for tok in SKIP_TOKENS):
                    continue

                # take text before first $ and use the last token as the part number
                price_match = PRICE_TOK.search(rest)
                before_price = rest[:price_match.start()] if price_match else rest
                tokens = [t for t in re.split(r"\s+", before_price) if t]
                cand = None
                for t in reversed(tokens):
                    if re.fullmatch(r"[A-Za-z0-9._/\-]+", t):
                        cand = t
                        break
                if cand:
                    items.append((qty, cand))
    return items

# ============================ Sheets logic ============================
def approve_parts_by_project(sheets, tracker_tab: str, project: str, subject: str, items: List[Tuple[int,str]]):
    tracker_tab, idx = _load_existing_tracker(sheets, tracker_tab)
    unmatched_tab = _ensure_unmatched_tab(sheets)

    res = sheets.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=f"{tracker_tab}!A2:I").execute()
    values = res.get("values", [])

    pn_col = idx.get("Part Number"); qty_col = idx.get("Quantity"); status_col = idx.get("Status")
    if pn_col is None or status_col is None:
        raise RuntimeError("Tracker headers must include 'Part Number' and 'Status'.")

    part_to_rows: Dict[str, List[int]] = {}
    for r, row in enumerate(values):
        pn = _norm(row[pn_col]) if pn_col < len(row) else ""
        if pn: part_to_rows.setdefault(pn, []).append(r)

    to_update: Dict[int, List[str]] = {}
    unmatched: List[Tuple[str, str, str, str]] = []

    for rfpo_qty, part in items:
        matches = part_to_rows.get(part, [])
        if not matches:
            unmatched.append((datetime.datetime.now().isoformat(timespec="seconds"), project, subject, f"{rfpo_qty}", part))
            if DEBUG: print(f"[RFPO] UNMATCHED: {part} (qty {rfpo_qty})")
            continue

        if not ALLOCATE_BY_QTY:
            rows_to_touch = matches
            if DEBUG: print(f"[RFPO] Approving ALL rows for {part} (matches={len(matches)})")
        else:
            remaining = rfpo_qty
            pending_first = [r for r in matches if status_col >= len(values[r]) or _norm(values[r][status_col]) != "Approved"]
            already = [r for r in matches if r not in pending_first]
            rows_to_touch = []
            for r in pending_first + already:
                if remaining <= 0: break
                row_qty = 1
                if qty_col is not None and qty_col < len(values[r]):
                    try: row_qty = int(float(values[r][qty_col]))
                    except Exception: row_qty = 1
                if status_col < len(values[r]) and _norm(values[r][status_col]) == "Approved":
                    continue
                rows_to_touch.append(r)
                remaining -= row_qty
            if DEBUG:
                used = rfpo_qty - max(remaining,0)
                print(f"[RFPO] Allocated {used}/{rfpo_qty} for {part} across {len(rows_to_touch)} row(s).")

        for r in rows_to_touch:
            row = list(values[r]) if r < len(values) else []
            while len(row) <= status_col: row.append("")
            if _norm(row[status_col]) != "Approved":
                row[status_col] = "Approved"
                to_update[r] = row

    if to_update:
        data = [{"range": f"{tracker_tab}!A{2+r}:I{2+r}", "values": [row[:9] + ([''] * max(0, 9-len(row)))]}
                for r, row in sorted(to_update.items())]
        sheets.spreadsheets().values().batchUpdate(spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": data}).execute()
        if DEBUG: print(f"[RFPO] Approved {len(to_update)} row(s).")
    else:
        if DEBUG: print("[RFPO] No status changes were needed.")

    if unmatched:
        sheets.spreadsheets().values().append(spreadsheetId=SPREADSHEET_ID, range=f"{unmatched_tab}!A2:F",
            valueInputOption="USER_ENTERED",
            body={"values": [[ts, project, subject, qty, part, "Part not found on tracker"]
                             for (ts, project, subject, qty, part) in unmatched]}).execute()
        if DEBUG: print(f"[RFPO] Logged {len(unmatched)} unmatched item(s).")

# ============================ Main ============================
if __name__ == "__main__":
    gmail, sheets = setup_services()
    if not gmail or not sheets: raise SystemExit(1)

    msgs = list_rfpo_messages(gmail)
    for m in msgs:
        try:
            subject, bodies, atts = _get_subject_bodies_attachments(gmail, m["id"])

            items = parse_rfpo_items_from_bodies(bodies)

            for fname, data in atts:
                if fname.lower().endswith(".pdf"):
                    pdf_items = parse_rfpo_items_from_pdf_bytes(data)
                    if DEBUG and pdf_items:
                        print(f"[RFPO] Parsed {len(pdf_items)} item(s) from PDF: {fname}")
                    items.extend(pdf_items)

            # Merge duplicate parts (sum qty)
            merged: Dict[str, int] = {}
            for qty, part in items:
                merged[part] = merged.get(part, 0) + int(qty)
            items = [(q, p) for p, q in merged.items()]

            if not items:
                if DEBUG:
                    print(f"[RFPO] REVIEW: no parsable line items in {subject}")
                continue

            resolution = resolve_subject_to_existing_tracker(
                sheets,
                SPREADSHEET_ID,
                subject,
                ["RFPO", "Request for Purchase Order"],
                parts=[p for _, p in items],
            )

            if not resolution.confirmed:
                if DEBUG:
                    print(
                        f"[RFPO] PROJECT {resolution.status}: {subject} "
                        f"method={resolution.method} suggestions={resolution.suggestions}"
                    )
                # No tracker creation and no approvals. Leave unread for review.
                continue

            project = resolution.canonical_project
            tracker_tab = resolution.tracker_title

            if DEBUG:
                print(f"[RFPO] {subject} -> {tracker_tab} :: {len(items)} unique item(s)")
                for q, p in items: print(f"   - ({q}) {p}")

            approve_parts_by_project(sheets, tracker_tab, project, subject, items)
            gmail.users().messages().modify(
                userId="me",
                id=m["id"],
                body={"removeLabelIds": ["UNREAD"]}
            ).execute()
        except Exception as e:
            print(f"[RFPO] Error processing message {m.get('id')}: {e}")
    print("PUMA4_RFPO complete.")
