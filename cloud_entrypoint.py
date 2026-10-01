import os
import sys
import json
import subprocess
import pathlib
from flask import Flask, jsonify
from lease import run_with_lease
from puma_runtime_config import TEST_MODE, validate_test_environment

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
        os.chmod(out_path, 0o600)
        print(f"[ENTRYPOINT] wrote {out_path.name} from {env_key}", flush=True)
        return True
    except Exception as e:
        print(f"[ENTRYPOINT] failed writing {out_path.name}: {e}", file=sys.stderr, flush=True)
        return False

def ensure_secrets() -> bool:
    """
    Materialize OAuth files when provided. Cloud/Test runs must never fall into
    interactive OAuth. Explicit GMAIL_* refresh-token credentials are also valid.
    """
    if (
        os.getenv("GMAIL_CLIENT_ID")
        and os.getenv("GMAIL_CLIENT_SECRET")
        and os.getenv("GMAIL_REFRESH_TOKEN")
    ):
        print("[ENTRYPOINT] using GMAIL_* refresh-token credentials", flush=True)
        return True

    _write_if_env("OAUTH_CLIENT_JSON", APP_DIR / "client_secrets.json")
    _write_if_env("OAUTH_TOKEN_JSON",  APP_DIR / "token.json")

    client_ok = (APP_DIR / "client_secrets.json").exists()
    token_ok = (APP_DIR / "token.json").exists()

    if not client_ok:
        print("ERROR: client_secrets.json not present and OAUTH_CLIENT_JSON not set.",
              file=sys.stderr, flush=True)
    if not token_ok:
        level = "ERROR" if (TEST_MODE or os.getenv("K_SERVICE")) else "WARNING"
        print(
            f"{level}: token.json missing. Cloud/Test cannot use interactive OAuth.",
            file=sys.stderr,
            flush=True,
        )

    if TEST_MODE or os.getenv("K_SERVICE"):
        return client_ok and token_ok
    return client_ok

def _has_noninteractive_credential_source() -> bool:
    env_refresh = bool(
        os.getenv("GMAIL_CLIENT_ID")
        and os.getenv("GMAIL_CLIENT_SECRET")
        and os.getenv("GMAIL_REFRESH_TOKEN")
    )
    oauth_env = bool(
        os.getenv("OAUTH_CLIENT_JSON")
        and os.getenv("OAUTH_TOKEN_JSON")
    )
    oauth_files = bool(
        (APP_DIR / "client_secrets.json").exists()
        and (APP_DIR / "token.json").exists()
    )
    return env_refresh or oauth_env or oauth_files


def static_readiness():
    """Validate routing and credential inputs without calling Google APIs."""
    try:
        validate_test_environment()
        if not _has_noninteractive_credential_source():
            raise RuntimeError("No non-interactive Google OAuth credential source is configured.")
        return True, "ready"
    except Exception as e:
        return False, str(e)


# ----------------- orchestration -----------------
def run_master() -> int:
    """Run the master orchestrator once; stream output; return exit code."""
    master = "PUMA_Master_v2.py"
    if not (APP_DIR / master).exists():
        raise RuntimeError("Hardened PUMA_Master_v2.py is missing from the runtime image")
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
    ok, detail = static_readiness()
    if ok:
        return "ok", 200
    return f"not ready: {detail}", 503

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
