import os
import unittest
from unittest import mock

import cloud_entrypoint as entry


class CloudEntrypointReadinessTests(unittest.TestCase):
    def test_readiness_rejects_missing_noninteractive_credentials(self):
        with mock.patch.object(entry, "validate_test_environment", return_value=None):
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(entry.Path if hasattr(entry, "Path") else entry.pathlib.Path, "exists", return_value=False):
                    ok, detail = entry.static_readiness()
        self.assertFalse(ok)
        self.assertIn("credential", detail.lower())

    def test_readiness_accepts_refresh_token_env_source(self):
        with mock.patch.object(entry, "validate_test_environment", return_value=None):
            with mock.patch.dict(
                os.environ,
                {
                    "GMAIL_CLIENT_ID": "client",
                    "GMAIL_CLIENT_SECRET": "secret",
                    "GMAIL_REFRESH_TOKEN": "refresh",
                },
                clear=True,
            ):
                ok, detail = entry.static_readiness()
        self.assertTrue(ok)
        self.assertEqual(detail, "ready")

    def test_readiness_surfaces_routing_validation_failure(self):
        with mock.patch.object(
            entry,
            "validate_test_environment",
            side_effect=RuntimeError("bad TEST target"),
        ):
            with mock.patch.dict(
                os.environ,
                {
                    "GMAIL_CLIENT_ID": "client",
                    "GMAIL_CLIENT_SECRET": "secret",
                    "GMAIL_REFRESH_TOKEN": "refresh",
                },
                clear=True,
            ):
                ok, detail = entry.static_readiness()
        self.assertFalse(ok)
        self.assertIn("bad TEST target", detail)


if __name__ == "__main__":
    unittest.main()
