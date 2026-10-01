#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PUMA_RFA.py
- Scan Gmail label for RFAs (unread only by default, excluding already-processed)
- Parse XLSX/CSV attachments + email body (qty/PN + "swap" detection)
- Append to "<Project> - Project Tracker" (auto-create tab if missing)
- Mark old PN as Omitted + strike-through columns C–E
- Mark processed threads with a Gmail label to avoid reprocessing
"""

import os
import io
import re
import sys
import csv
import time
import base64
import urllib.parse
import pandas as pd  # needs openpyxl

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from puma_project_resolver import resolve_subject_to_existing_tracker
from puma_runtime_config import required_env, test_safe_env, LIVE_PUMA_SPREADSHEET_ID

# ---------------------------------------------------------------------------
# CONFIG / ENVs
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]

SPREADSHEET_ID = required_env("PUMA_SPREADSHEET_ID", LIVE_PUMA_SPREADSHEET_ID)

RFA_LABEL_NAME = test_safe_env("PUMA_RFA_LABEL_NAME", "PUMA - RFA")
PROCESSED_LABEL_NAME = test_safe_env("PUMA_RFA_PROCESSED_LABEL_NAME", "PUMA - RFA - Processed")
PUMA_TRACKER_TEMPLATE_TAB = os.getenv("PUMA_TRACKER_TEMPLATE_TAB", "Project Tracker Template")

# Query defaults to unread; we’ll always exclude the processed label
RAW_QUERY = os.getenv("PUMA_RFA_QUERY", "is:unread")
DEBUG = os.getenv("DEBUG", "0") == "1"
REQUIRE_SUBJECT = os.getenv("PUMA_RFA_REQUIRE_SUBJECT", "1") not in ("0","false","False","no")

# Globals set in run()
TITLES_MAP = {}
PROCESSED_LABEL_ID = None

# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------

def _load_creds(token_file="token.json", client_secret="credentials.json"):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            if DEBUG: print("[RFA] Refreshing credentials…")
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as token:
            token.write(creds.to_json())
    return creds

def gmail_service():
    return build("gmail", "v1", credentials=_load_creds())

def sheets_service():
    return build("sheets", "v4", credentials=_load_creds())

# ---------------------------------------------------------------------------
# SMALL UTILITIES
# ---------------------------------------------------------------------------

def _normalize_label_name(s: str) -> str:
    if not s:
        return ""
    s = s.replace("—", "-").replace("–", "-")
    s = re.sub(r"\s+", " ", s.strip())
    return s.lower()

def _call_with_backoff(request_callable, *args, **kwargs):
    """Exponential backoff for Sheets 429s."""
    delay = 1.0
    for _ in range(6):
        try:
            return request_callable(*args, **kwargs)
        except HttpError as e:
            if getattr(e, "resp", None) and e.resp.status == 429:
                time.sleep(delay)
                delay = min(delay * 2, 10.0)
                continue
            raise

# ---------------------------------------------------------------------------
# GMAIL HELPERS
# ---------------------------------------------------------------------------

def find_label_id_by_name(gmail, fallback_name):
    """Resolves label by ID env, full name, or tail segment (nested)."""
    env_id = os.getenv("PUMA_RFA_LABEL_ID", "").strip()
    if env_id:
        return env_id

    want = _normalize_label_name(os.getenv("PUMA_RFA_LABEL_NAME", fallback_name))

    # log account
    try:
        prof = gmail.users().getProfile(userId="me").execute()
        if DEBUG:
            print(f"[RFA] Gmail profile: {prof.get('emailAddress')}")
    except Exception as e:
        print(f"[RFA][WARN] Could not get Gmail profile: {e}")

    resp = gmail.users().labels().list(userId="me").execute()
    labels = resp.get("labels", [])

    # exact
    for lbl in labels:
        if _normalize_label_name(lbl.get("name","")) == want:
            return lbl.get("id")

    # tail of nested path
    for lbl in labels:
        tail = lbl.get("name","").split("/")[-1]
        if _normalize_label_name(tail) == want:
            return lbl.get("id")

    print(f"[RFA] Label '{os.getenv('PUMA_RFA_LABEL_NAME', fallback_name)}' not found. Available labels:")
    for lbl in labels:
        print(f" - {lbl.get('name')}  (id: {lbl.get('id')})")
    return None

def ensure_label(gmail, name) -> str:
    """Return label id; create if missing."""
    want = name.strip()
    resp = gmail.users().labels().list(userId="me").execute()
    for lbl in resp.get("labels", []):
        if lbl.get("name") == want:
            return lbl.get("id")
    body = {"name": want, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
    created = gmail.users().labels().create(userId="me", body=body).execute()
    return created.get("id")

def gmail_link_from_headers(headers, thread_id):
    msgid = next((h.get("value") for h in headers if h["name"].lower() == "message-id"), None)
    if msgid:
        return f'https://mail.google.com/mail/u/0/#search/rfc822msgid:{urllib.parse.quote(msgid)}'
    return f'https://mail.google.com/mail/u/0/#inbox/{thread_id}'

def get_email_body_text(payload):
    texts = []
    def walk(p):
        if "parts" in p:
            for sp in p["parts"]:
                yield from walk(sp)
        else:
            yield p
    for p in walk(payload):
        mime = p.get("mimeType","")
        data = p.get("body",{}).get("data")
        if not data: 
            continue
        try:
            t = base64.urlsafe_b64decode(data).decode("utf-8","ignore")
        except Exception:
            t = base64.urlsafe_b64decode(data + "===").decode("utf-8","ignore")
        if mime.startswith("text/html"):
            t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
            t = re.sub(r"(?is)<br\s*/?>", "\n", t)
            t = re.sub(r"(?is)</p>", "\n", t)
            t = re.sub(r"(?is)<.*?>", " ", t)
        texts.append(t)
    return "\n".join(texts)

# ---------------------------------------------------------------------------
# BODY PARSER
# ---------------------------------------------------------------------------

def looks_like_rfa_subject(subject):
    return bool(re.search(r'\bRFA\b', subject, re.IGNORECASE))

def parse_items_from_text(text):
    """Return (items, omit_pns)."""
    items, omit_pns = [], []

    # e.g. "swapping the (7) OLDPN ... to (9) NEWPN"
    swap_re = re.compile(
        r'swapp\w*\s+the\s+\(?\s*(\d{1,5})\s*\)?\s*([A-Z0-9\-]{5,})\b.*?\bto\b.*?\(?\s*(\d{1,5})\s*\)?\s*([A-Z0-9\-]{5,})',
        re.IGNORECASE | re.DOTALL
    )
    m = swap_re.search(text)
    if m:
        _old_qty, old_pn, new_qty, new_pn = m.groups()
        omit_pns.append(old_pn.strip())
        items.append({"Part Number": new_pn.strip(), "Quantity": int(new_qty), "Type": "", "Manufacturer": ""})

    # lines like "(9) PN", "9x PN", "QTY 9 PN"
    line_re = re.compile(
        r'^\s*(?:\(|\b)?\s*(\d{1,5})\s*(?:\)|\b)?\s*(?:x|qty[: ]*)?\s*([A-Z][A-Z0-9\-]{4,})\b',
        re.IGNORECASE | re.MULTILINE
    )
    for qty, pn in line_re.findall(text):
        pn = pn.strip()
        if not any(pn == i["Part Number"] for i in items):
            items.append({"Part Number": pn, "Quantity": int(qty), "Type": "", "Manufacturer": ""})

    return items, omit_pns

# ---------------------------------------------------------------------------
# ATTACHMENTS
# ---------------------------------------------------------------------------

def read_csv_bytes(b: bytes):
    rows = []
    s = io.StringIO(b.decode("utf-8","ignore"))
    rdr = csv.DictReader(s)
    for r in rdr:
        rows.append(r)
    return rows

def read_xlsx_bytes_to_rows(b: bytes):
    rows = []
    with io.BytesIO(b) as bio:
        df = pd.read_excel(bio, engine="openpyxl")
    for _, r in df.fillna("").iterrows():
        rows.append({k: str(v).strip() for k, v in r.items()})
    return rows

def rows_to_items(rows):
    mapped = []
    for r in rows:
        pn = r.get("Part Number") or r.get("PartNumber") or r.get("Part") or r.get("PN") or r.get("Item") or ""
        qty = r.get("Quantity") or r.get("Qty") or r.get("QTY") or r.get("QTY Ordered") or r.get("QTY Req") or ""
        mfr = r.get("Manufacturer") or r.get("MFR") or r.get("Brand") or ""
        typ = r.get("Type") or r.get("Line Type") or ""
        if pn and qty:
            try:
                q = int(str(qty).strip().replace(",",""))
            except Exception:
                continue
            mapped.append({"Part Number": str(pn).strip(), "Quantity": q, "Type": str(typ).strip(), "Manufacturer": str(mfr).strip()})
    return mapped

# ---------------------------------------------------------------------------
# SHEETS HELPERS
# ---------------------------------------------------------------------------

def sheet_titles_map(svc):
    meta = _call_with_backoff(svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute)
    out = {}
    for sh in meta.get("sheets", []):
        props = sh.get("properties", {})
        out[props.get("title")] = props.get("sheetId")
    return out

def create_tracker_tab_if_missing(svc, titles_map: dict, project_name: str) -> int:
    """Ensure '<project> - Project Tracker' exists (duplicate template if present)."""
    tab_title = f"{project_name} - Project Tracker"
    if tab_title in titles_map:
        return titles_map[tab_title]

    ss = SPREADSHEET_ID
    template_id = titles_map.get(PUMA_TRACKER_TEMPLATE_TAB)

    if template_id:
        resp = _call_with_backoff(
            svc.spreadsheets().batchUpdate(
                spreadsheetId=ss,
                body={"requests":[{"duplicateSheet":{
                    "sourceSheetId": template_id,
                    "insertSheetIndex": 999,
                    "newSheetName": tab_title
                }}]}
            ).execute
        )
        new_id = resp["replies"][0]["duplicateSheet"]["properties"]["sheetId"]
        titles_map[tab_title] = new_id
        return new_id

    # Minimal new tab with headers
    resp = _call_with_backoff(
        svc.spreadsheets().batchUpdate(
            spreadsheetId=ss,
            body={"requests":[{"addSheet":{"properties":{
                "title": tab_title,
                "gridProperties":{"frozenRowCount":1}
            }}}]}
        ).execute
    )
    new_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    headers = [
        "Project","Source","Type","Part Number","Manufacturer","Quantity",
        "Status","PO Number","Estimated Ship Date (ESD)",
        "Date Received","Date Scheduled","Date Delivered"
    ]
    _call_with_backoff(
        svc.spreadsheets().values().update(
            spreadsheetId=ss,
            range=f"{tab_title}!A1:L1",
            valueInputOption="USER_ENTERED",
            body={"values":[headers]}
        ).execute
    )

    titles_map[tab_title] = new_id
    return new_id

def apply_omissions_and_append(svc, titles_map, tracker_tab, project_name, data_items, omit_pns, src_link, src_text):
    # RFA is an operational update. It may only modify a CONFIRMED existing tracker.
    tab = tracker_tab
    sheet_id = titles_map.get(tab)
    if not sheet_id:
        raise RuntimeError(f"Confirmed tracker no longer exists: {tab}")

    # Mark omissions by searching col D
    if omit_pns:
        colD = _call_with_backoff(
            svc.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID, range=f"{tab}!D2:D"
            ).execute
        ).get("values", [])
        requests, value_updates = [], []
        for idx, v in enumerate(colD, start=2):
            pn = (v[0] if v else "").strip()
            if pn in omit_pns:
                # Strike-through columns C–E
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": idx - 1,
                            "endRowIndex": idx,
                            "startColumnIndex": 2,  # C
                            "endColumnIndex": 5     # E (exclusive)
                        },
                        "cell": {"userEnteredFormat": {"textFormat": {"strikethrough": True}}},
                        "fields": "userEnteredFormat.textFormat.strikethrough"
                    }
                })
                value_updates.append({"range": f"{tab}!G{idx}", "values": [["Omitted"]]})

        if requests:
            _call_with_backoff(
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=SPREADSHEET_ID, body={"requests": requests}
                ).execute
            )
        if value_updates:
            _call_with_backoff(
                svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=SPREADSHEET_ID,
                    body={"valueInputOption": "USER_ENTERED", "data": value_updates}
                ).execute
            )

    if not data_items:
        return

    values = []
    for it in data_items:
        values.append([
            project_name,
            f'=HYPERLINK("{src_link}","{src_text}")',
            it.get("Type",""),
            it.get("Part Number",""),
            it.get("Manufacturer",""),
            it.get("Quantity",""),
            "Unapproved",
            "",
            "",
            "",
            "",
            "",
        ])

    _call_with_backoff(
        svc.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{tab}!A2:L",
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute
    )
    print(f"[RFA] Sheet '{tab}' updated with {len(values)} row(s).")

# ---------------------------------------------------------------------------
# END-TO-END: ONE MESSAGE
# ---------------------------------------------------------------------------

def process_message(gmail, sheets, msg_ref):
    msg = gmail.users().messages().get(userId="me", id=msg_ref["id"], format="full").execute()
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")

    if REQUIRE_SUBJECT and not looks_like_rfa_subject(subject):
        if DEBUG: print(f"[RFA][SKIP] Not an RFA: {subject}")
        # Still mark as processed? No — skip entirely so team can adjust subject if needed.
        return

    src_link = gmail_link_from_headers(headers, msg.get("threadId"))
    date_hdr = next((h["value"] for h in headers if h["name"] == "Date"), "")

    parsed_items, omit_pns = [], []

    # Attachments
    def walk(part):
        if "parts" in part:
            for p in part["parts"]:
                yield from walk(p)
        else:
            yield part
    for part in walk(payload):
        filename = part.get("filename") or ""
        if not filename:
            continue
        attach_id = part.get("body", {}).get("attachmentId")
        if not attach_id:
            continue
        if filename.lower().endswith((".csv",".xlsx")):
            att = gmail.users().messages().attachments().get(
                userId="me", messageId=msg_ref["id"], id=attach_id
            ).execute()
            data = base64.urlsafe_b64decode(att["data"])
            try:
                rows = read_csv_bytes(data) if filename.lower().endswith(".csv") else read_xlsx_bytes_to_rows(data)
                parsed_items.extend(rows_to_items(rows))
                if DEBUG: print(f"[RFA] Parsed {len(parsed_items)} item(s) from {filename}")
            except Exception as e:
                print(f"[RFA][WARN] Failed to parse {filename}: {e}")

    # Body
    try:
        body_text = get_email_body_text(payload)
        body_items, body_omits = parse_items_from_text(body_text)
        parsed_items.extend(body_items)
        omit_pns.extend(body_omits)
    except Exception as e:
        print(f"[RFA][WARN] Body parse error: {e}")

    # Normalize
    norm_items = []
    for it in parsed_items:
        qty_raw = str(it.get("Quantity","")).strip()
        qty_val = int(qty_raw) if qty_raw.isdigit() else qty_raw
        norm_items.append({
            "Part Number": it.get("Part Number","").strip(),
            "Quantity": qty_val,
            "Type": it.get("Type",""),
            "Manufacturer": it.get("Manufacturer",""),
        })

    evidence_parts = [
        it.get("Part Number", "")
        for it in norm_items
        if it.get("Part Number", "")
    ] + list(set(omit_pns))

    resolution = resolve_subject_to_existing_tracker(
        sheets,
        SPREADSHEET_ID,
        subject,
        ["RFA", "Request for Adder", "Request for Adders"],
        parts=evidence_parts,
    )

    if not resolution.confirmed:
        if DEBUG:
            print(
                f"[RFA] PROJECT {resolution.status}: {subject} "
                f"method={resolution.method} suggestions={resolution.suggestions}"
            )
        # Do not create a tracker, do not omit/append rows, and do not mark
        # processed. Leave the message available for human review.
        return

    project_name = resolution.canonical_project
    tracker_tab = resolution.tracker_title
    src_text = f"RFA – {project_name} {date_hdr}".strip()

    apply_omissions_and_append(
        sheets,
        TITLES_MAP,
        tracker_tab,
        project_name,
        norm_items,
        list(set(omit_pns)),
        src_link,
        src_text,
    )

    # Mark read + processed so we never touch it again
    gmail.users().messages().modify(
        userId="me", id=msg_ref["id"],
        body={"removeLabelIds": ["UNREAD"], "addLabelIds": [PROCESSED_LABEL_ID]}
    ).execute()

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run():
    gm = gmail_service()
    sh = sheets_service()
    if DEBUG: print("[RFA] Running PUMA_RFA with DEBUG=1")

    # Resolve labels
    label_id = find_label_id_by_name(gm, RFA_LABEL_NAME)
    if not label_id:
        print(f"ERROR: Gmail label '{os.getenv('PUMA_RFA_LABEL_NAME', RFA_LABEL_NAME)}' not found.")
        return

    global PROCESSED_LABEL_ID
    PROCESSED_LABEL_ID = ensure_label(gm, PROCESSED_LABEL_NAME)

    # Cache sheet titles once
    global TITLES_MAP
    TITLES_MAP = sheet_titles_map(sh)

    # Build query: unread only (or whatever RAW_QUERY is) BUT always exclude processed label
    raw = (RAW_QUERY or "").strip()
    # treat "ALL/*" as empty if someone sets it; we still add the exclusion
    if raw.upper() in ("ALL","ANY","NONE","BLANK","*","NULL"):
        raw = ""
    exclude = f'-label:"{PROCESSED_LABEL_NAME}"'
    query = exclude if raw == "" else f"{raw} {exclude}"

    page_token = None
    total = 0

    while True:
        list_args = {
            "userId": "me",
            "labelIds": [label_id],
            "maxResults": 500,
        }
        if query != "":
            list_args["q"] = query
        if page_token:
            list_args["pageToken"] = page_token

        resp = gm.users().messages().list(**list_args).execute()
        msgs = resp.get("messages", [])
        if not msgs:
            if total == 0 and DEBUG:
                print("[RFA] No messages matched label/query.")
            break

        total += len(msgs)
        if DEBUG:
            print(f"[RFA] Fetched {len(msgs)} message refs (running total {total})")

        for m in msgs:
            try:
                process_message(gm, sh, m)
            except HttpError as e:
                print(f"[RFA][ERROR] Gmail/Sheets API: {e}")
            except Exception as e:
                print(f"[RFA][ERROR] Unexpected: {e}")

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if DEBUG:
        print("[RFA] Done scanning label.")

if __name__ == "__main__":
    run()
