
"""
PUMA_Master.py — One-click orchestrator for the full PUMA flow.

Runs in this order (per your process):
1) OKD   -> build trackers + seed rows
2) RFA   -> append adder requests to existing tracker
3) RFPO  -> approve items (body + PDF)
4) PO    -> upload PO PDF + set "Ordered" + PO hyperlink
5) RR    -> upload Receiving Report PDF + set "Received" + Date Received hyperlink
6) RFPS  -> set "Scheduled" + Date Scheduled hyperlink
7) DR    -> upload Delivery Report PDF + set "Delivered" + Date Delivered hyperlink

How it requests ALL permissions at once:
- First, we run a one-time "bootstrap_auth" using the union of all scopes used across the steps.
- This writes token.json with every needed scope in a single OAuth flow.
- Then each step script runs and simply reuses token.json (no additional prompts).

Requirements:
- Put this file in the same folder as your step scripts:
  PUMA13_OKD.py, PUMA3_RFA.py, PUMA4_RFPO.py, PUMA17_PO.py, PUMA6_RR.py, PUMA5_RFPS.py, PUMA1_DR.py
- Ensure client_secrets.json is in the same folder (Google OAuth credentials).

Optional flags you can pass to this orchestrator:
  --debug     : passes --debug to step scripts that support it
  --nodebug   : passes --nodebug to step scripts that support it
  --dry-run   : print what would run, but skip executing the steps
"""

import sys, os, subprocess, shlex
from typing import List

# ====== Configure the step scripts ======
STEPS = [
    ("OKD",   "PUMA13_OKD.py",  []),
    ("RFA",   "PUMA3_RFA.py",   []),
    ("RFPO",  "PUMA4_RFPO.py",  []),
    ("PO",    "PUMA17_PO.py",   []),
    ("RR",    "PUMA6_RR.py",    []),
    ("RFPS",  "PUMA5_RFPS.py",  []),
    ("DR",    "PUMA1_DR.py",    []),
]

# ====== Scopes: UNION of all steps (Gmail read+modify, Sheets, Drive) ======
UNION_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def bootstrap_auth():
    """
    Perform a one-time OAuth flow requesting the UNION_SCOPES and write token.json.
    If token.json already exists and includes these scopes, we skip re-auth.
    """
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
    except Exception as e:
        # Install deps on the fly if needed
        print("[AUTH] Installing Google auth dependencies...")
        os.system(f"{sys.executable} -m pip install --quiet google-api-python-client google-auth-httplib2 google-auth-oauthlib")
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request

    token_path = "token.json"
    creds = None
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, UNION_SCOPES)
            if creds and creds.valid:
                print("[AUTH] Existing token.json already covers all required scopes. ✔")
                return
            if creds and creds.expired and creds.refresh_token:
                print("[AUTH] Refreshing existing token...")
                creds.refresh(Request())
                with open(token_path, "w") as f:
                    f.write(creds.to_json())
                print("[AUTH] Token refreshed. ✔")
                return
            else:
                print("[AUTH] Existing token.json is missing scopes or cannot refresh; re-authorizing...")
        except Exception:
            print("[AUTH] token.json present but not usable for the union scopes; re-authorizing...")

    if not os.path.exists("client_secrets.json"):
        print("ERROR: client_secrets.json not found in the current folder.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file("client_secrets.json", UNION_SCOPES)
    creds = flow.run_local_server(port=0)
    with open(token_path, "w") as f:
        f.write(creds.to_json())
    print("[AUTH] token.json created with ALL required scopes. ✔")

def run_steps(debug_flag: str = ""):
    dry_run = ("--dry-run" in sys.argv)
    for label, script, extra_args in STEPS:
        script_path = os.path.join(os.getcwd(), script)
        if not os.path.isfile(script_path):
            print(f"[{label}] SKIP — {script} not found in {os.getcwd()}")
            continue

        cmd_parts: List[str] = [sys.executable, script_path]
        if debug_flag:
            cmd_parts.append(debug_flag)
        cmd_parts.extend(extra_args)

        cmd = " ".join(shlex.quote(p) for p in cmd_parts)
        if dry_run:
            print(f"[{label}] (dry-run) {cmd}")
            continue

        print(f"\n— — — — — — — — — — — — — — — — — —")
        print(f"[{label}] Running: {cmd}")
        print(f"— — — — — — — — — — — — — — — — — —")
        try:
            # Inherit stdout so you see each step's logs in real time
            subprocess.run(cmd_parts, check=True)
            print(f"[{label}] Completed ✔")
        except subprocess.CalledProcessError as e:
            print(f"[{label}] ERROR (exit {e.returncode}). Continuing to next step...")

if __name__ == "__main__":
    # Auth once with the union of scopes so all steps can reuse token.json
    bootstrap_auth()

    # Pick a debug flag to pass through (if provided)
    passthrough = ""
    if "--debug" in sys.argv:
        passthrough = "--debug"
    elif "--nodebug" in sys.argv:
        passthrough = "--nodebug"

    # Run the steps in sequence
    run_steps(debug_flag=passthrough)
