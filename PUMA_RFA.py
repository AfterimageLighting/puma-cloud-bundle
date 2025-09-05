import os
import io
import base64
import pandas as pd
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from openpyxl import load_workbook

# ============================ Config ============================
SPREADSHEET_ID = '1pwVlYSGVjyTCLt4GT7xU2TCnxfdJuxAbp_jU6Snisls'
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/spreadsheets'
]
DEBUG = True  # set False to quiet debug prints

# ====================== Auth / Services ========================
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

# ===================== Header utilities ========================
def _n(x): return (str(x) if x is not None else "").strip().lower()
NEEDED_KEYS = ("Quantity", "Type", "Manufacturer", "Part Number")

def _find_header_map_on_row(values):
    """Detect our 4 columns on a header row. Supports combined 'Manufacturer / Product Number'."""
    idx_map = {}
    for idx, v in enumerate(values):
        t = _n(v)
        if t in {"qty", "quantity", "qnty", "qty ordered", "qty/ea"}:
            idx_map["Quantity"] = idx
        elif t in {"type", "item type", "category"}:
            idx_map["Type"] = idx
        elif ("manufacturer" in t) and ("product" in t):
            idx_map["Manufacturer"] = idx
            idx_map["Part Number"] = idx + 1
        elif t in {"manufacturer", "mfr", "mfg", "brand", "vendor"}:
            idx_map["Manufacturer"] = idx
        elif t in {"part number", "part #", "part no", "item #", "sku", "item", "item no"}:
            idx_map["Part Number"] = idx
    score = sum(1 for k in NEEDED_KEYS if k in idx_map)
    return idx_map if score >= 3 else None

# ================== XLSX / CSV readers (master sheet) ==========
def _read_master_sheet_xlsx(file_stream, project_name, filename):
    wb = load_workbook(file_stream, data_only=True)
    ws = None
    for s in wb.worksheets:
        if _n(s.title) == "master quotation":
            ws = s
            break
    if ws is None:
        ws = wb.worksheets[0]
        if DEBUG:
            print(f"[DEBUG] '{filename}': 'master quotation' not found; using '{ws.title}'")

    header_row, header_map = None, None
    max_check = min(200, ws.max_row or 200)
    for r in range(1, max_check + 1):
        vals = [ws.cell(r, c).value for c in range(1, ws.max_column + 1)]
        if not any(vals):
            continue
        cand = _find_header_map_on_row(vals)
        if cand:
            header_row, header_map = r, cand
            break

    if not header_map:
        if DEBUG:
            print(f"[DEBUG] '{filename}': no usable header row on '{ws.title}'")
        return []

    if DEBUG:
        print(f"[DEBUG] '{filename}': using '{ws.title}' header row {header_row} map {header_map}")

    rows = []
    r = header_row + 1
    empty_streak = 0
    while r <= (ws.max_row or r + 500):
        row = [ws.cell(r, c).value for c in range(1, ws.max_column + 1)] if r <= ws.max_row else []
        if not row or not any(row):
            empty_streak += 1
            if empty_streak >= 3:
                break
            r += 1
            continue
        empty_streak = 0

        def pick(key, default=""):
            idx = header_map.get(key)
            if idx is None or idx >= len(row):
                return default
            v = row[idx]
            return "" if v is None else v

        line = {
            "Project": project_name.strip(),
            "Source": filename,
            "Type": pick("Type", ""),
            "Part Number": str(pick("Part Number", "")).strip(),
            "Manufacturer": pick("Manufacturer", ""),
            "Quantity": pick("Quantity", ""),
            "Status": "Unapproved",
            "PO Number": "",
            "Estimated Ship Date (ESD)":"",
            "Date Received":"",
            "Date Scheduled":"",
            "Date Delivered":""
        }
        if any([line["Part Number"], line["Manufacturer"], line["Type"]]):
            rows.append(line)
        r += 1
    return rows

def _read_master_sheet_csv(file_bytes, project_name, filename):
    df = pd.read_csv(io.BytesIO(file_bytes))
    cols = list(df.columns)
    rename = {}
    for i, c in enumerate(cols):
        t = _n(c)
        if t in {"qty", "quantity", "qnty", "qty ordered", "qty/ea"}:
            rename[c] = "Quantity"
        elif t in {"type", "item type", "category"}:
            rename[c] = "Type"
        elif ("manufacturer" in t) and ("product" in t):
            rename[c] = "Manufacturer"
            if i + 1 < len(cols):
                rename[cols[i + 1]] = "Part Number"
        elif t in {"manufacturer", "mfr", "mfg", "brand", "vendor"}:
            rename[c] = "Manufacturer"
        elif t in {"part number", "part #", "part no", "item #", "sku", "item", "item no"}:
            rename[c] = "Part Number"
    if rename:
        df = df.rename(columns=rename)

    out = []
    for _, rec in df.iterrows():
        line = {
            "Project": project_name.strip(),
            "Source": filename,
            "Type": rec.get("Type", ""),
            "Part Number": str(rec.get("Part Number", "")).strip(),
            "Manufacturer": rec.get("Manufacturer", ""),
            "Quantity": rec.get("Quantity", ""),
            "Status": "Unapproved",
            "PO Number": "",
            "Estimated Ship Date (ESD)":"",
            "Date Received":"",
            "Date Scheduled":"",
            "Date Delivered":""
        }
        if any([line["Part Number"], line["Manufacturer"], line["Type"]]):
            out.append(line)
    return out

# =========================== Gmail =============================
def get_emails(gmail_service):
    print("Fetching emails from PUMA inbox...")
    out = []
    req = gmail_service.users().messages().list(
        userId="me",
        labelIds=["Label_216878436602330283"],   # <-- put actual PUMA - RFA label ID here
        q="is:unread has:attachment",
        maxResults=100
    )
    while req is not None:
        resp = req.execute()
        out.extend(resp.get("messages", []))
        req = gmail_service.users().messages().list_next(previous_request=req, previous_response=resp)
    print(f"Found {len(out)} unread RFA email(s).") if out else print("No new RFA emails found.")
    return out

def _is_rfa_subject(s: str) -> bool:
    if not s:
        return False
    su = s.strip().upper()
    return su.startswith("RFA")  # guard variants implicitly

def get_and_process_emails_single(gmail_service, message_ref):
    try:
        msg = gmail_service.users().messages().get(userId="me", id=message_ref["id"], format="full").execute()
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")

        # Hard gate: ignore non-RFA messages entirely and leave them unread
        if not _is_rfa_subject(subject):
            if DEBUG:
                print(f"[SKIP] Not an RFA email: {subject}")
            return None, []

        project_name = subject.split(" - ", 1)[1] if " - " in subject else subject

        parsed = []

        def walk(part):
            if "parts" in part:
                for p in part["parts"]:
                    yield from walk(p)
            else:
                yield part

        for part in walk(payload):
            filename = part.get("filename") or ""
            if not filename or not filename.lower().endswith((".xlsx", ".csv")):
                continue
            attach_id = part.get("body", {}).get("attachmentId")
            if not attach_id:
                continue
            att = gmail_service.users().messages().attachments().get(
                userId="me", messageId=message_ref["id"], id=attach_id
            ).execute()
            file_data = base64.urlsafe_b64decode(att["data"])
            file_stream = io.BytesIO(file_data)

            if filename.lower().endswith(".xlsx"):
                rows = _read_master_sheet_xlsx(file_stream, project_name, filename)
            else:
                rows = _read_master_sheet_csv(file_data, project_name, filename)
            parsed.extend(rows)

        # Mark email as read only after successful parse
        gmail_service.users().messages().modify(
            userId="me", id=message_ref["id"], body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        print(f"Processed {len(parsed)} row(s) from: {subject}")
        return project_name, parsed

    except HttpError as e:
        print(f"Gmail API error on {message_ref.get('id')}: {e}")
        return "(unknown project)", []
    except Exception as e:
        print(f"Unexpected error on {message_ref.get('id')}: {e}")
        return "(unknown project)", []

# ========================== Sheets =============================
def sheet_titles_map(sheets_service):
    meta = sheets_service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    return {s['properties']['title']: s['properties']['sheetId'] for s in meta.get('sheets', [])}

def _is_tax_or_total(line: dict) -> bool:
    for key in ("Type", "Part Number", "Manufacturer"):
        val = _n(line.get(key, ""))
        if any(tok in val for tok in ("tax", "total")):
            return True
    return False

def update_existing_project_tracker(sheets_service, project_name, data_rows):
    tab_tracker = f"{project_name} - Project Tracker"
    titles = sheet_titles_map(sheets_service)

    if tab_tracker not in titles:
        print(f"[WARN] Project tracker tab not found for '{project_name}'. Expected tab named: {tab_tracker}. Skipping append.")
        return

    if not data_rows:
        print("No data to append.")
        return

    filtered = [d for d in data_rows if not _is_tax_or_total(d)]
    if not filtered:
        print("All parsed rows were filtered (tax/total). Nothing to append.")
        return

    values = []
    for d in filtered:
        values.append([
            d.get('Project',''), d.get('Source',''), d.get('Type',''),
            d.get('Part Number',''), d.get('Manufacturer',''), d.get('Quantity',''),
            d.get('Status',''), d.get('PO Number',''), d.get('Estimated Ship Date (ESD)',''),
            d.get('Date Received',''), d.get('Date Scheduled',''), d.get('Date Delivered','')
        ])

    sheets_service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID, range=f"{tab_tracker}!A2:L",
        valueInputOption='USER_ENTERED', body={'values': values}
    ).execute()
    print(f"Sheet '{tab_tracker}' updated with {len(values)} row(s).")


# ============================ Main =============================
if __name__ == '__main__':
    try:
        import openpyxl  # noqa
    except ImportError:
        print("openpyxl not found. Installing...")
        os.system("pip install openpyxl")
        import openpyxl  # noqa

    gmail_service, sheets_service = setup_services()
    if gmail_service and sheets_service:
        messages = get_emails(gmail_service)
        if messages:
            for m in messages:
                try:
                    project_name, rows = get_and_process_emails_single(gmail_service, m)
                    if project_name is None:
                        continue
                    update_existing_project_tracker(sheets_service, project_name, rows)
                except Exception as e:
                    print(f"Error processing a message; continuing. Details: {e}")
    print("PUMA1_RFA automation finished.")
