import os
import uuid
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Dict, Any

# If set to "1", Firestore is completely bypassed (useful for local runs).
PUMA_DISABLE_LEASE = os.getenv("PUMA_DISABLE_LEASE") == "1"

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

class LeaseBusy(Exception):
    """Raised when another run already holds the lease."""
    pass

# ----------------- No-op lease (used when disabled or Firestore unavailable) -----------------
class NoopLease:
    def __init__(self):
        self.run_id = uuid.uuid4().hex[:8]
        self.holder = f"noop:{self.run_id}"

    def acquire(self, *_, **__) -> bool:
        return True

    def heartbeat(self, *_ , **__):
        pass

    def release(self):
        pass

# ----------------- Firestore-backed lease -----------------
class FsLease:
    def __init__(self, project: Optional[str] = None, collection: str = "leases", doc: str = "puma-master"):
        # Import lazily so module import works even if package is absent.
        from google.cloud import firestore  # type: ignore
        self.client = firestore.Client(project=project)
        self.doc = self.client.collection(collection).document(doc)
        self.run_id = uuid.uuid4().hex[:8]
        self.holder = f"{os.getenv('K_SERVICE','local')}:{self.run_id}"

    def acquire(self, lease_secs: int) -> bool:
        now = _utcnow()
        expire_at = now + timedelta(seconds=lease_secs)

        def txn_op(txn):
            snap = txn.get(self.doc)
            if snap.exists:
                data = snap.to_dict() or {}
                cur_holder = data.get("holder")
                cur_expiry = data.get("expire_at")
                # Firestore returns a datetime (timezone-aware) if written as such
                if isinstance(cur_expiry, datetime) and cur_expiry.tzinfo is None:
                    cur_expiry = cur_expiry.replace(tzinfo=timezone.utc)
                if cur_expiry and cur_expiry > now:
                    # someone else holds it
                    return False
            txn.set(self.doc, {
                "holder": self.holder,
                "run_id": self.run_id,
                "acquired_at": now,
                "expire_at": expire_at
            })
            return True

        return self.client.transaction()(txn_op)

    def heartbeat(self, extend_secs: int):
        now = _utcnow()
        self.doc.update({
            "last_heartbeat": now,
            "expire_at": now + timedelta(seconds=extend_secs),
        })

    def release(self):
        # make release idempotent — set expire in the past but keep the record for debugging
        self.doc.set({
            "holder": None,
            "released_at": _utcnow(),
            "expire_at": _utcnow() - timedelta(seconds=1),
            "run_id": self.run_id,
        }, merge=True)

# ----------------- Lease Orchestrator -----------------
def _make_lease() -> object:
    """
    Return a lease object that supports acquire(lease_secs), heartbeat(extend_secs), release()
    Preference order:
      1) No-op if PUMA_DISABLE_LEASE=1
      2) Firestore if google-cloud-firestore is importable
      3) No-op fallback
    """
    if PUMA_DISABLE_LEASE:
        return NoopLease()
    try:
        # Try to create a Firestore client; if it fails, fall back
        return FsLease()
    except Exception as e:
        print(f"[LEASE] Firestore unavailable, using no-op lease: {e}", flush=True)
        return NoopLease()

def run_with_lease(
    do_work: Callable[..., Dict[str, Any]],
    lease_secs: int = 900
) -> Dict[str, Any]:
    """
    Acquire a lease, run do_work(run_id=..., holder=...), send heartbeats, and release.
    Returns a JSON-serializable dict explaining what happened.
    """
    lease = _make_lease()

    # No-op lease: just run
    if isinstance(lease, NoopLease):
        result = do_work(run_id=lease.run_id, holder=lease.holder)
        return {"ok": True, "skipped": False, "result": result, "lease": "noop"}

    # Firestore-backed
    try:
        acquired = lease.acquire(lease_secs=lease_secs)
    except Exception as e:
        # If Firestore errors, do not block the run — behave like no-op to keep the pipeline alive
        print(f"[LEASE] acquire error, falling back to run anyway: {e}", flush=True)
        result = do_work(run_id="fallback", holder="fallback")
        return {"ok": True, "skipped": False, "result": result, "lease": "error-fallback"}

    if not acquired:
        return {"ok": True, "skipped": True, "reason": "busy"}

    stop = threading.Event()

    def _hb():
        while not stop.wait(lease_secs * 0.4):
            try:
                lease.heartbeat(extend_secs=lease_secs)
            except Exception:
                # Heartbeat failures shouldn't kill the run
                pass

    t = threading.Thread(target=_hb, daemon=True)
    t.start()
    try:
        result = do_work(run_id=lease.run_id, holder=lease.holder)
        return {"ok": True, "skipped": False, "result": result}
    finally:
        stop.set()
        t.join(timeout=2.0)
        try:
            lease.release()
        except Exception:
            pass
