
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wrapper runner for the PO stage.
Your orchestrator can execute this file directly:
    python PUMA_PO.py [--include-read] [--days-back N]

It builds Gmail/Drive/Sheets clients using ADC and calls run() in PUMA_PO_clean.
"""

import argparse
import os
import sys

from googleapiclient.discovery import build
import google.auth

import PUMA_PO_clean as po  # contains the actual logic

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

def get_services():
    creds, _ = google.auth.default(scopes=SCOPES)
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return gmail, drive, sheets

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-read", action="store_true", help="Backfill mode; include read messages")
    parser.add_argument("--days-back", type=int, default=None, help="Only fetch emails newer than N days")
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging")      # <— add
    parser.add_argument("--nodebug", action="store_true", help="Disable verbose logging")    # <— add
    args = parser.parse_args(argv)

    # Toggle debug if flags are passed from the master
    if args.debug:
        po.DEBUG = True
    if args.nodebug:
        po.DEBUG = False

    # Required env vars for the underlying module
    missing = []
    if not os.getenv("PUMA_SPREADSHEET_ID"):
        missing.append("PUMA_SPREADSHEET_ID")
    if not os.getenv("PUMA_PO_DRIVE_FOLDER_ID"):
        missing.append("PUMA_PO_DRIVE_FOLDER_ID")
    if missing:
        print(f"[PO] ERROR: Missing required env var(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    gmail, drive, sheets = get_services()
    if po.DEBUG:
        print(f"[PO] Starting run | include_read={args.include_read} | days_back={args.days_back}")
    po.run(gmail, drive, sheets, include_read=args.include_read, days_back=args.days_back)
    if po.DEBUG:
        print("[PO] Completed OK")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
