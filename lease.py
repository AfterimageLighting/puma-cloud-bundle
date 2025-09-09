import os
import uuid
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

PUMA_DISABLE_LEASE = os.getenv("PUMA_DISABLE_LEASE") == "1"

def _utcnow():
    return datetime.now(timezone.utc)

class LeaseBusy(Exception):
    """Raised when another run already holds the lease."""
    pass

# ---------- No-op lease (used when disabled or Firestore not present) ----------
class NoopLease:
    def __init__(self):
        self.holder = f"noop:{uuid.uuid4().hex[:8]}"
        self.run_id = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    def try_acquire(self, lease_secs: int = 900): return True
    def heartbeat(self, extend_secs: int = 900): pass
    def release(self): pass

def run_with_lease(do_work, lease_secs: int = 900):
    """
    Wraps a job with a Firestore-backed lease.
    If PUMA_DISABLE_LEASE=1 or Firestore is unavailable, runs immediately.
    """

    if PUMA_DISABLE_LEASE:
        l = NoopLease()
        return {"ok": True, "skipped": False, "result": do_work(run_id=l.run_id, holder=l.holder)}

    try:
        from google.cloud import firestore
    except Exception:
        # Firestore not installed or not available
        l = NoopLease()
        return {"ok": True, "skipped": False, "result": do_work(run_id=l.run_id, holder=l.holder)}

    # ---------- Real Firestore lease ----------
    class Lease:
        def __init__(self, db: Optional[firestore.Client] = None):
            self.db = db or firestore.Client()
            self.doc = self.db.collection("puma_control").document("lease")
            self.holder = f"puma-runner:{uuid.uuid4().hex[:8]}"
            self.run_id = datetime.utcnow().strftime("%Y%m%d%H%M%S")

        def try_acquire(self, lease_secs: int = 900) -> bool:
            @firestore.transactional
            def _txn(tx: firestore.Transaction):
                snap = tx.get(self.doc)
                now = _utcnow()
                locked_until = snap.get("locked_until") if snap.exists else None
                if locked_until and locked_until > now:
                    return False
                new_until = now + timedelta(seconds=lease_secs)
                data = {
                    "locked_until": new_until,
                    "holder": self.holder,
                    "run_id": self.run_id,
                    "started_at": now,
                    "last_heartbeat": now,
                    "version": (snap.get("version", 0) + 1) if snap.exists else 1,
                }
                tx.set(self.doc, data, merge=True)
                return True
            return _txn(self.db.transaction())

        def heartbeat(self, extend_secs: int = 900):
            now = _utcnow()
            new_until = now + timedelta(seconds=extend_secs)
            @firestore.transactional
            def _txn(tx: firestore.Transaction):
                snap = tx.get(self.doc)
                if not snap.exists or snap.get("holder") != self.holder:
                    return
                tx.update(self.doc, {"locked_until": new_until, "last_heartbeat": now})
            _txn(self.db.transaction())

        def release(self):
            now = _utcnow()
            @firestore.transactional
            def _txn(tx: firestore.Transaction):
                snap = tx.get(self.doc)
                if not snap.exists or snap.get("holder") != self.holder:
                    return
                tx.update(self.doc, {"locked_until": now, "holder": None, "run_id": None})
            _txn(self.db.transaction())

    l = Lease()
    if not l.try_acquire(lease_secs=lease_secs):
        return {"ok": True, "skipped": True, "reason": "busy"}

    stop = threading.Event()
    def _hb():
        try:
            while not stop.wait(60):  # heartbeat every 60s
                l.heartbeat(extend_secs=lease_secs)
        except Exception:
            pass

    t = threading.Thread(target=_hb, daemon=True)
    t.start()
    try:
        result = do_work(run_id=l.run_id, holder=l.holder)
        return {"ok": True, "skipped": False, "result": result}
    finally:
        stop.set()
        t.join(timeout=2.0)
        try: l.release()
        except Exception: pass
