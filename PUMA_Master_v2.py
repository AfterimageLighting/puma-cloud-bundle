#!/usr/bin/env python3
"""
PUMA_Master_v2.py — Orchestrator with single OAuth, lockfile, logging, retries,
env-based step filtering, and summary.

Default step order: OKD, RFA, RFPO, PO, RR, RFPS, DR

To constrain steps at deploy/runtime (no code changes):
  - Allow-list:  PUMA_ALLOWED_STEPS=OKD,PO,RFPS
  - Block-list:  PUMA_DISABLED_STEPS=RFA,RFPO,RR,DR

CLI still supported:
  --steps OKD,PO,RFPS
  --timeout-sec 900
  --retries 1
  --debug / --nodebug
  --dry-run
"""
import os
import sys
import json
import time
import shlex
import subprocess
import datetime
from typing import List, Dict

# ----------------------------
# Pipeline definition
# ----------------------------
STEPS = [
    ("OKD",   "PUMA_OKD.py",  []),
    ("RFA",   "PUMA_RFA.py",  []),
    ("RFPO",  "PUMA_RFPO.py", []),
    ("PO",    "PUMA_PO.py",   [
        # Label comes from PUMA_PO_LABEL_NAME so TEST mode cannot be
        # overridden by a production label hard-coded in the orchestrator.
        "--only-unread",
        "--require-subject-po",
        "--exclude-rfpo",
        "--mark-read",
    ]),
    ("RR",    "PUMA_RR.py",   []),
    ("RFPS",  "PUMA_RFPS.py", []),
    ("DR",    "PUMA_DR.py",   []),
]

# ----------------------------
# Google auth bootstrap (union scopes)
# ----------------------------
UNION_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def _now_ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

def bootstrap_auth() -> None:
    # Fast-path for service-style creds provided via env (used by your Cloud Run)
    if (
        os.getenv("GMAIL_CLIENT_ID")
        and os.getenv("GMAIL_CLIENT_SECRET")
        and os.getenv("GMAIL_REFRESH_TOKEN")
    ):
        print("[AUTH] ENV credentials detected; skipping token.json bootstrap.")
        return

    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
    except Exception:
        print("[AUTH] Installing Google auth dependencies...")
        os.system(
            f"{sys.executable} -m pip install --quiet "
            "google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        )
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request

    token_path = "token.json"
    creds = None
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, UNION_SCOPES)
            if creds and creds.valid:
                print("[AUTH] Existing token.json already covers all required scopes. ✓")
                return
            if creds and creds.expired and creds.refresh_token:
                print("[AUTH] Refreshing existing token...")
                creds.refresh(Request())
                with open(token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                print("[AUTH] Token refreshed. ✓")
                return
            else:
                print("[AUTH] Existing token.json missing scopes or cannot refresh; re-authorizing...")
        except Exception:
            print("[AUTH] token.json present but not usable for the union scopes; re-authorizing...")

    if not os.path.exists("client_secrets.json"):
        print("ERROR: client_secrets.json not found in the current folder.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file("client_secrets.json", UNION_SCOPES)
    creds = flow.run_local_server(port=0)
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print("[AUTH] token.json created with ALL required scopes. ✓")

# ----------------------------
# Logging and helpers
# ----------------------------
class TeeLogger:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.file = open(path, "w", encoding="utf-8", errors="replace")
    def write(self, s: str):
        sys.stdout.write(s)
        self.file.write(s)
        self.file.flush()
    def close(self):
        self.file.close()

def parse_emails_found(line: str) -> int:
    l = line.strip()
    if "Found" in l and "email(s)" in l:
        import re
        m = re.search(r"Found\s+(\d+)\s+email\(s\)", l)
        if m:
            return int(m.group(1))
    if "No new emails found" in l:
        return 0
    return -1

def acquire_lock(lock_path: str) -> None:
    try:
        with open(lock_path, "x", encoding="utf-8") as f:
            f.write(f"locked at {_now_ts()} pid={os.getpid()}")
    except FileExistsError:
        print(f"Another PUMA master appears to be running (lock: {lock_path}). Exiting.")
        sys.exit(2)

def release_lock(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass

def run_step(
    label: str,
    script: str,
    extra_args: List[str],
    debug_flag: str,
    timeout_sec: int,
    retries: int,
    logger: TeeLogger,
    dry_run: bool,
) -> Dict:
    result = {
        "label": label,
        "script": script,
        "status": "skipped" if dry_run else "unknown",
        "return_code": None,
        "emails_found_guess": None,
        "duration_sec": 0.0,
        "attempts": 0,
    }
    script_path = os.path.join(os.getcwd(), script)
    if not os.path.isfile(script_path):
        logger.write(f"[{label}] SKIP — {script} not found in {os.getcwd()}\n")
        return result

    cmd_parts: List[str] = [sys.executable, script_path]
    if debug_flag:
        cmd_parts.append(debug_flag)
    cmd_parts.extend(extra_args)

    cmd = " ".join(shlex.quote(p) for p in cmd_parts)
    if dry_run:
        logger.write(f"[{label}] (dry-run) {cmd}\n")
        return result

    for attempt in range(1, retries + 1):
        start = time.time()
        result["attempts"] = attempt
        logger.write("\n— — — — — — — — — — — — — — — — —\n")
        logger.write(f"[{label}] Running: {cmd}\n")
        logger.write("— — — — — — — — — — — — — — — — —\n")

        proc = subprocess.Popen(
            cmd_parts,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        step_emails_guess = None
        try:
            while True:
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    logger.write(line)
                    guess = parse_emails_found(line)
                    if guess != -1:
                        step_emails_guess = guess
            rc = proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            proc.kill()
            logger.write(f"[{label}] TIMEOUT after {timeout_sec}s. Killing process.\n")
            rc = 124
        end = time.time()

        result["duration_sec"] += round(end - start, 2)
        result["return_code"] = rc
        result["emails_found_guess"] = step_emails_guess

        if rc == 0:
            logger.write(f"[{label}] Completed ✓\n")
            result["status"] = "ok"
            break
        else:
            logger.write(f"[{label}] ERROR (exit {rc}).\n")
            if attempt < retries:
                delay = min(30, 2 ** attempt)
                logger.write(f"[{label}] Retrying in {delay}s...\n")
                time.sleep(delay)
            else:
                result["status"] = "failed"

    return result

# ----------------------------
# Env parsing helpers (NEW)
# ----------------------------
def _parse_csv_env(name: str):
    v = os.getenv(name, "").strip()
    if not v:
        return None
    return {s.strip().upper() for s in v.split(",") if s.strip()}

# ----------------------------
# Main
# ----------------------------
def main():
    debug_flag = ""
    if "--debug" in sys.argv:
        debug_flag = "--debug"
    elif "--nodebug" in sys.argv:
        debug_flag = "--nodebug"

    dry_run = ("--dry-run" in sys.argv)

    try:
        retries = int(sys.argv[sys.argv.index("--retries")+1]) if "--retries" in sys.argv else 1
    except Exception:
        retries = 1
    try:
        timeout_sec = int(sys.argv[sys.argv.index("--timeout-sec")+1]) if "--timeout-sec" in sys.argv else 15 * 60
    except Exception:
        timeout_sec = 15 * 60

    # CLI step filter (still supported)
    selected_steps = None
    if "--steps" in sys.argv:
        try:
            selected_steps = [
                s.strip().upper()
                for s in sys.argv[sys.argv.index("--steps") + 1].split(",")
                if s.strip()
            ]
        except Exception:
            print("Bad --steps value. Use like: --steps OKD,PO,RR")
            sys.exit(3)

    # NEW: env-based gating (only applied when --steps is NOT supplied)
    allowed_set  = _parse_csv_env("PUMA_ALLOWED_STEPS")
    disabled_set = _parse_csv_env("PUMA_DISABLED_STEPS")

    run_id = _now_ts()
    logs_dir = os.path.join(os.getcwd(), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, f"PUMA_Run_{run_id}.log")
    lock_path = os.path.join(os.getcwd(), "PUMA_MASTER.lock")
    logger = TeeLogger(log_path)

    try:
        acquire_lock(lock_path)
        logger.write(f"[MASTER] Run ID: {run_id}\n")
        logger.write(f"[MASTER] Log file: {log_path}\n")

        bootstrap_auth()

        steps_to_run = []
        for label, script, extra in STEPS:
            # If CLI --steps provided, that takes precedence
            if selected_steps is not None and label not in selected_steps:
                continue

            # Otherwise, consider env filters
            if selected_steps is None:
                if allowed_set is not None and label not in allowed_set:
                    logger.write(f"[MASTER] Skipping {label} (not in PUMA_ALLOWED_STEPS)\n")
                    continue
                if disabled_set is not None and label in disabled_set:
                    logger.write(f"[MASTER] Skipping {label} (listed in PUMA_DISABLED_STEPS)\n")
                    continue

            steps_to_run.append((label, script, extra))

        summary = {"run_id": run_id, "started_at": run_id, "steps": [], "overall_status": "ok"}

        for label, script, extra in steps_to_run:
            res = run_step(label, script, extra, debug_flag, timeout_sec, retries, logger, dry_run)
            summary["steps"].append(res)
            if res["status"] == "failed":
                summary["overall_status"] = "partial_failure"

        json_path = os.path.join(logs_dir, f"PUMA_Run_{run_id}_summary.json")
        txt_path  = os.path.join(logs_dir, f"PUMA_Run_{run_id}_summary.txt")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"PUMA run summary ({run_id})\n")
            f.write(f"Overall: {summary['overall_status']}\n\n")
            for s in summary["steps"]:
                f.write(
                    f"- {s['label']}: {s['status']}  "
                    f"(rc={s['return_code']}, emails~={s['emails_found_guess']}, "
                    f"duration={s['duration_sec']}s, attempts={s['attempts']})\n"
                )

        logger.write("\n[MASTER] Summary written:\n")
        logger.write(f"  {json_path}\n  {txt_path}\n")
        logger.write("[MASTER] Done.\n")

    finally:
        release_lock(lock_path)
        logger.close()

if __name__ == "__main__":
    main()
