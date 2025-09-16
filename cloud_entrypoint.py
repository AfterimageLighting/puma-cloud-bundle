import os, sys, json, subprocess, threading, pathlib
from flask import Flask, request, jsonify, Response
from lease import run_with_lease

def _puma_job(run_id: str, holder: str):
    """
    Called inside the lease. Returns a dict you’ll see in the HTTP response.
    """
    if not ensure_secrets():
        # Non-fatal: still return 200 from the handler; body will show the issue.
        return {"ok": False, "error": "missing client_secrets.json or token.json"}

    rc = run_master()
    return {"ok": (rc == 0), "rc": rc, "run_id": run_id, "holder": holder}

APP_DIR = pathlib.Path(__file__).parent
app = Flask(__name__)

def write_if_env(env_key: str, out_path: pathlib.Path):
    val = os.getenv(env_key, "").strip()
    if not val:
        return False
    try:
        try:
            data = json.loads(val)
            out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except json.JSONDecodeError:
            out_path.write_text(val, encoding="utf-8")
        print(f"[ENTRYPOINT] Wrote {out_path.name} from {env_key}", flush=True)
        return True
    except Exception as e:
        print(f"[ENTRYPOINT] Failed writing {out_path.name}: {e}", file=sys.stderr, flush=True)
        return False

def ensure_secrets():
    wrote_client = write_if_env("OAUTH_CLIENT_JSON", APP_DIR / "client_secrets.json")
    wrote_token  = write_if_env("OAUTH_TOKEN_JSON",  APP_DIR / "token.json")
    if not (APP_DIR / "client_secrets.json").exists():
        print("ERROR: client_secrets.json not present and OAUTH_CLIENT_JSON not set.", file=sys.stderr, flush=True)
        return False
    if not (APP_DIR / "token.json").exists():
        print("WARNING: token.json missing. Generate locally and provide via OAUTH_TOKEN_JSON.", file=sys.stderr, flush=True)
    return True

def run_master():
    """Run the master orchestrator once; stream output to logs; return exit code."""
    master = "PUMA_Master_v2.py" if (APP_DIR / "PUMA_Master_v2.py").exists() else "PUMA_Master.py"
    args = os.getenv("PUMA_ARGS", "").strip()
    cmd = [sys.executable, str(APP_DIR / master)]
    if args:
        cmd += args.split()
    print(f"[ENTRYPOINT] Launching: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    for line in proc.stdout:
        print(line, end="", flush=True)
    rc = proc.wait()
    print(f"[ENTRYPOINT] Exit code: {rc}", flush=True)
    return rc

@app.get("/healthz")
def health():
    return "ok", 200

# NEW: simple root that does NOT run anything
@app.get("/")
def root():
    return jsonify({
        "service": "puma-orchestrator",
        "status": "ready",
        "routes": ["/healthz", "/run"]
    }), 200

# ONLY /run kicks off the orchestrator under a Firestore lease
@app.get("/run")
def handle_run():
    lease_secs = int(os.getenv("PUMA_LEASE_SECS", "900"))
    outcome = run_with_lease(_puma_job, lease_secs=lease_secs)
    return (json.dumps(outcome), 200, {"Content-Type": "application/json"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    # Bind to 0.0.0.0 so Cloud Run can reach it
    app.run(host="0.0.0.0", port=port)
