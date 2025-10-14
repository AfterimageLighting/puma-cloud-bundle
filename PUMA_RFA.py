#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PUMA_RFA.py
Parse RFA emails and update Project Tracker tabs:
- Append new items from XLSX/CSV attachments or email body text
- Detect "swap" requests, mark old PN rows as Omitted and strike through (cols C-E)

Author: PUMA
"""

import os
import io
import re
import sys
import base64
import urllib.parse
from datetime import datetime

# Third-party
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# Optional helpers
import csv
import pandas as pd  # for xlsx (openpyxl engine)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets"
]

SPREADSHEET_ID = os.getenv("PUMA_SPREADSHEET_ID", "").strip()
if not SPREADSHEET_ID:
    print("ERROR: PUMA_SPREADSHEET_ID is not set.")
    sys.exit(1)

RFA_LABEL_NAME = os.getenv("PUMA_RFA_LABEL_NAME", "PUMA - RFA")
DEBUG = os.getenv("DEBUG", "0") == "1"

# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------

def _load_creds(token_file="token.json", client_secret="credentials.json"):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            if DEBUG: print("Refreshing credentials…")
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
# GMAIL HELPERS
# ---------------------------------------------------------------------------

def find_label_id_by_name(gmail, name):
    resp = gmail.users().labels().list(userId="me").execute()
    for lbl in resp.get("labels", []):
        if lbl.get("name") == name:
            return lbl.get("id")
    return None

def gmail_link_from_headers(headers, thread_id):
    msgid = next((h.get("value") for h in headers if h["name"].lower() == "message-id"), None)
    if msgid:
        return f'https://mail.google.com/mail/u/0/#search/rfc822msgid:{urllib.parse.quote(msgid)}'
    return f'https://mail.google.com/mail/u/0/#inbox/{thread_id}'

def get_email_body_text(payload):
    """Collects text/plain and text/html parts, decodes, returns concatenated text."""
    texts = []

    def walk(p):
        if "parts" in p:
            for sp in p["parts"]:
                yield from walk(sp)
        else:
            yield p

    for p in walk(payload):
        mime = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if not data:
            continue
        # decode
        try:
            t = base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
        except Exception:
            t = base64.urlsafe_b64decode(data + "===").decode("utf-8", "ignore")
        if mime.startswith("text/html"):
            # very light html cleanup
            t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
            t = re.sub(r"(?is)<br\s*/?>", "\n", t)
            t = re.sub(r"(?is)</p>", "\n", t)
            t = re.sub(r"(?is)<.*?>", " ", t)
        texts.append(t)

    return "\n".join(texts)

# ---------------------------------------------------------------------------
# BODY PARSER
# ---------------------------------------------------------------------------

def parse_items_from_text(text):
    """
    Returns: (items, omit_pns)
      items: list of dicts {"Part Number","Quantity","Type","Manufacturer"}
      omit_pns: list[str]
    """
    items, omit_pns = [], []

    # Detect swap, e.g. "swapping the (7) ABC123 ... to (9) XYZ456"
    swap_re = re.compile(
        r'swapp\w*\s+the\s+\(?\s*(\d{1,5})\s*\)?\s*([A-Z0-9\-]{5,})\b.*?\bto\b.*?\(?\s*(\d{1,5})\s*\)?\s*([A-Z0-9\-]{5,})',
        re.IGNORECASE | re.DOTALL
    )
    m = swap_re.search(text)
    if m:
        old_qty, old_pn, new_qty, new_pn = m.groups()
        omit_pns.append(old_pn.strip())
        items.append({"Part Number": new_pn.strip(), "Quantity": int(new_qty), "Type": "", "Manufacturer": ""})

    # General patterns on separate lines:
    # "(9) PN", "9x PN", "QTY 9 PN"
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
# ATTACHMENT PARSERS (basic)
# ---------------------------------------------------------------------------

def read_csv_bytes(b: bytes):
    rows = []
    s = io.StringIO(b.decode("utf-8", "ignore"))
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
    """
    Try to map common column names to the schema. Extend this map as you see examples.
    """
    mapped = []
    for r in rows:
        pn = r.get("Part Number") or r.get("PartNumber") or r.get("Part") or r.get("PN") or r.get("Item") or ""
        qty = r.get("Quantity") or r.get("Qty") or r.get("QTY") or r.get("QTY Ordered") or r.get("QTY Req") or ""
        mfr = r.get("Manufacturer") or r.get("MFR") or r.get("Brand") or ""
        typ = r.get("Type") or r.get("Line Type") or ""
        if pn and qty:
            try:
                q = int(str(qty).strip().replace(",", ""))
            except Exception:
                continue
            mapped.append({"Part Number": str(pn).strip(), "Quantity": q, "Type": str(typ).strip(), "Manufacturer": str(mfr).strip()})
    return mapped

# ---------------------------------------------------------------------------
# SHEETS HELPERS
# ---------------------------------------------------------------------------

def sheet_titles_map(svc):
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    out = {}
    for sh in meta.get("sheets", []):
        props = sh.get("properties", {})
        out[props.get("title")] = props.get("sheetId")
    return out

def apply_omissions_and_append(svc, project_name, data_items, omit_pns, src_link, src_text):
    tab = f"{project_name} - Project Tracker"
    titles = sheet_titles_map(svc)
    if tab not in titles:
        print(f"[WARN] Missing tracker tab: {tab}")
        return
    sheet_id = titles[tab]

    # mark omissions: search column D (Part Number)
    if omit_pns:
        colD = svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"{tab}!D2:D"
        ).execute().get("values", [])
        requests, value_updates = [], []
        for idx, v in enumerate(colD, start=2):
            pn = (v[0] if v else "").strip()
            if pn in omit_pns:
                # Strike-through C, D, E (0-indexed: C=2, E=4)
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": idx - 1,
                            "endRowIndex": idx,
                            "startColumnIndex": 2,
                            "endColumnIndex": 5
                        },
                        "cell": {"userEnteredFormat": {"textFormat": {"strikethrough": True}}},
                        "fields": "userEnteredFormat.textFormat.strikethrough"
                    }
                })
                # Set Status (G) to Omitted
                value_updates.append({"range": f"{tab}!G{idx}", "values": [["Omitted"]]})

        if requests:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=SPREADSHEET_ID, body={"requests": requests}
            ).execute()
        if value_updates:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"valueInputOption": "USER_ENTERED", "data": value_updates}
            ).execute()

    # Append new rows
    if not data_items:
        return

    values = []
    for it in data_items:
        values.append([
            project_name,
            f'=HYPERLINK("{src_link}","{src_text}")',
            it.get("Type", ""),
            it.get("Part Number", ""),
            it.get("Manufacturer", ""),
            it.get("Quantity", ""),
            "Unapproved",  # Status
            "",  # PO Number
            "",  # Estimated Ship Date (ESD)
            "",  # Date Received
            "",  # Date Scheduled
            ""   # Date Delivered
        ])

    svc.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{tab}!A2:L",
        valueInputOption="USER_ENTERED",
        body={"values": values}
    ).execute()

    print(f"Sheet '{tab}' updated with {len(values)} row(s).")

# ---------------------------------------------------------------------------
# EMAIL -> PARSE -> SHEETS
# ---------------------------------------------------------------------------

def looks_like_rfa_subject(subject):
    # Accept "RFA - <Project>" or "RFA – <Project>" or "[RFA] <Project>"
    return bool(re.search(r'\bRFA\b', subject, re.IGNORECASE))

def process_message(gmail, sheets, msg_ref):
    msg = gmail.users().messages().get(userId="me", id=msg_ref["id"], format="full").execute()
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
    if not looks_like_rfa_subject(subject):
        if DEBUG: print(f"[SKIP] Not an RFA: {subject}")
        return

    # Project Name: split at first " - " or " – "
    project_name = subject
    if " - " in subject:
        project_name = subject.split(" - ", 1)[1].strip()
    elif " – " in subject:
        project_name = subject.split(" – ", 1)[1].strip()

    # Source link + text
    src_link = gmail_link_from_headers(headers, msg.get("threadId"))
    date_hdr = next((h["value"] for h in headers if h["name"] == "Date"), "")
    src_text = f"RFA – {project_name} {date_hdr}".strip()

    parsed_items = []
    omit_pns = []

    # 1) Parse structured attachments
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
        body = part.get("body", {})
        attach_id = body.get("attachmentId")
        if not attach_id:
            continue

        if filename.lower().endswith(".csv") or filename.lower().endswith(".xlsx"):
            att = gmail.users().messages().attachments().get(
                userId="me", messageId=msg_ref["id"], id=attach_id
            ).execute()
            data = base64.urlsafe_b64decode(att["data"])
            try:
                if filename.lower().endswith(".csv"):
                    rows = read_csv_bytes(data)
                else:
                    rows = read_xlsx_bytes_to_rows(data)
                parsed_items.extend(rows_to_items(rows))
                if DEBUG: print(f"Parsed {len(parsed_items)} item(s) from {filename}")
            except Exception as e:
                print(f"[WARN] Failed to parse {filename}: {e}")

    # 2) Parse email body
    try:
        body_text = get_email_body_text(payload)
        body_items, body_omits = parse_items_from_text(body_text)
        parsed_items.extend(body_items)
        omit_pns.extend(body_omits)
    except Exception as e:
        print(f"[WARN] Body parse error: {e}")

    # Normalize to our append schema
    norm_items = []
    for it in parsed_items:
        norm_items.append({
            "Part Number": it.get("Part Number", "").strip(),
            "Quantity": int(it.get("Quantity", 0)) if str(it.get("Quantity","")).strip().isdigit() else it.get("Quantity",""),
            "Type": it.get("Type", ""),
            "Manufacturer": it.get("Manufacturer", "")
        })

    # Write to Sheets (omissions first)
    apply_omissions_and_append(sheets, project_name, norm_items, list(set(omit_pns)), src_link, src_text)

    # Mark email as read
    gmail.users().messages().modify(
        userId="me", id=msg_ref["id"], body={"removeLabelIds": ["UNREAD"]}
    ).execute()

def run():
    gm = gmail_service()
    sh = sheets_service()

    label_id = find_label_id_by_name(gm, RFA_LABEL_NAME)
    if not label_id:
        print(f"ERROR: Gmail label '{RFA_LABEL_NAME}' not found.")
        return

    req = gm.users().messages().list(
        userId="me",
        labelIds=[label_id],
        q="is:unread",
        maxResults=100
    )
    resp = req.execute()
    msgs = resp.get("messages", [])

    if not msgs:
        if DEBUG: print("No unread RFA messages.")
        return

    for m in msgs:
        try:
            process_message(gm, sh, m)
        except HttpError as e:
            print(f"[ERROR] Gmail/Sheets API: {e}")
        except Exception as e:
            print(f"[ERROR] Unexpected: {e}")

if __name__ == "__main__":
    run()
