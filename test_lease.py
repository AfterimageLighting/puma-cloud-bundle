import unittest
from unittest import mock

import lease


class LeaseSafetyTests(unittest.TestCase):
    def test_noop_propagates_inner_failure(self):
        with mock.patch.object(lease, "_make_lease", return_value=lease.NoopLease()):
            result = lease.run_with_lease(
                lambda **_: {"ok": False, "error": "synthetic failure"}
            )
        self.assertFalse(result["ok"])
        self.assertFalse(result["skipped"])

    def test_test_mode_rejects_disabled_lease(self):
        with mock.patch.object(lease, "TEST_MODE", True):
            with mock.patch.object(lease, "PUMA_DISABLE_LEASE", True):
                with self.assertRaises(RuntimeError):
                    lease._make_lease()

    def test_test_mode_does_not_run_after_acquire_error(self):
        class BrokenLease:
            def acquire(self, lease_secs):
                raise RuntimeError("firestore down")

        called = {"value": False}

        def work(**_):
            called["value"] = True
            return {"ok": True}

        with mock.patch.object(lease, "TEST_MODE", True):
            with mock.patch.object(lease, "_make_lease", return_value=BrokenLease()):
                result = lease.run_with_lease(work)

        self.assertFalse(result["ok"])
        self.assertFalse(called["value"])


if __name__ == "__main__":
    unittest.main()
