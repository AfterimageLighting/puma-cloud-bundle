import os
import sys
import json
import subprocess
import pathlib
from flask import Flask, jsonify
from lease import run_with_lease

APP_DIR = pathlib.Path(__file__).parent
app = Flask(__name__)

# ----------------- helpers to materialize secrets from env (optional) -----------------
def _write_if_env(env_key: str, out_path: pathlib.Path) -> bool:
    """If env_key exists, write its JSON/text value to out_path."""
    val = os.getenv(env_key, "").strip()
    if not val:
        return False
    try:
        try:
            data = json.loads(val)
            out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except json.JSONDecodeError:
            out_path.write_text(val, encoding="utf-8")
        print(f"[ENTRYPOINT] wrote {out_path.name} from {env_key}", flush=True)
        return True
    except Exception as e:
        print(f"[ENTRYPOINT] failed writing {out_path.name}: {e}", file=sys.stderr, flush=True)
        return False

def ensure_secrets() -> bool:
    """
    Best-effort: write client_secrets.json and token.json from env.
    Non-fatal if token.json is missing; fatal only if client_secrets.json is missing.
    """
    wrote_client = _write_if_env("OAUTH_CLIENT_JSON", APP_DIR / "client_secrets.json")
    wrote_token  = _write_if_env("OAUTH_TOKEN_JSON",  APP_DIR / "token.json")

    ok = True
    if not (APP_DIR / "client_secrets.json").exists():
        print("ERROR: client_secrets.json not present and OAUTH_CLIENT_JSON not set.",
              file=sys.stderr, flush=True)
        ok = False
    if not (APP_DIR / "token.json").exists():
        print("WARNING: token.json missing. Provide OAUTH_TOKEN_JSON to avoid re-auth.",
              file=sys.stderr, flush=True)
    return ok

# ----------------- orchestration -----------------
def run_master() -> int:
    """Run the master orchestrator once; stream output; return exit code."""
    master = "PUMA_Master_v2.py" if (APP_DIR / "PUMA_Master_v2.py").exists() else "PUMA_Master.py"
    args = os.getenv("PUMA_ARGS", "").strip()
    cmd = [sys.executable, str(APP_DIR / master)]
    if args:
        cmd += args.split()

    print(f"[ENTRYPOINT] launching: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=str(APP_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace"
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
    rc = proc.wait()
    print(f"[ENTRYPOINT] exit code: {rc}", flush=True)
    return rc

def _puma_job(run_id: str, holder: str):
    """Called inside the (possibly Firestore-backed) lease."""
    if not ensure_secrets():
        # Non-fatal: return a body explaining the issue; HTTP status stays 200.
        return {"ok": False, "error": "missing client_secrets.json or token.json"}

    rc = run_master()
    return {"ok": (rc == 0), "rc": rc, "run_id": run_id, "holder": holder}

# ----------------- HTTP routes -----------------
@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/")
def root():
    # keep it simple; do not kick work on startup
    return jsonify({
        "service": "puma-orchestrator",
        "status": "ready",
        "note": "Use /run to trigger one orchestrator pass. This page is just a health surface."
    })

@app.get("/run")
def run_once():
    # Default lease 15 min, configurable
    lease_secs = int(os.getenv("PUMA_LEASE_SECS", "900"))
    outcome = run_with_lease(_puma_job, lease_secs=lease_secs)
    # Always return 200 so Scheduler doesn't retry; the body explains what happened.
    return jsonify(outcome), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
