import os
import unittest
from unittest import mock

import puma_runtime_config as cfg


class PumaRuntimeConfigTests(unittest.TestCase):
    def test_test_mode_requires_explicit_value(self):
        with mock.patch.object(cfg, "TEST_MODE", True):
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError):
                    cfg.test_safe_env("PUMA_SPREADSHEET_ID", cfg.LIVE_PUMA_SPREADSHEET_ID)

    def test_test_mode_rejects_production_target(self):
        with mock.patch.object(cfg, "TEST_MODE", True):
            with mock.patch.dict(
                os.environ,
                {"PUMA_SPREADSHEET_ID": cfg.LIVE_PUMA_SPREADSHEET_ID},
                clear=True,
            ):
                with self.assertRaises(RuntimeError):
                    cfg.test_safe_env("PUMA_SPREADSHEET_ID", cfg.LIVE_PUMA_SPREADSHEET_ID)

    def test_test_mode_accepts_isolated_target(self):
        with mock.patch.object(cfg, "TEST_MODE", True):
            with mock.patch.dict(
                os.environ,
                {"PUMA_SPREADSHEET_ID": "TEST_SPREADSHEET"},
                clear=True,
            ):
                self.assertEqual(
                    cfg.test_safe_env(
                        "PUMA_SPREADSHEET_ID",
                        cfg.LIVE_PUMA_SPREADSHEET_ID,
                    ),
                    "TEST_SPREADSHEET",
                )

    def test_production_mode_allows_legacy_default(self):
        with mock.patch.object(cfg, "TEST_MODE", False):
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    cfg.test_safe_env(
                        "PUMA_SPREADSHEET_ID",
                        cfg.LIVE_PUMA_SPREADSHEET_ID,
                    ),
                    cfg.LIVE_PUMA_SPREADSHEET_ID,
                )


if __name__ == "__main__":
    unittest.main()
