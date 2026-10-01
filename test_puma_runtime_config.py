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

    def test_test_mode_rejects_unverified_target(self):
        with mock.patch.object(cfg, "TEST_MODE", True):
            with mock.patch.dict(
                os.environ,
                {"PUMA_SPREADSHEET_ID": "SOME_OTHER_TEST_SHEET"},
                clear=True,
            ):
                with self.assertRaises(RuntimeError):
                    cfg.test_safe_env(
                        "PUMA_SPREADSHEET_ID",
                        cfg.LIVE_PUMA_SPREADSHEET_ID,
                    )

    def test_validate_test_environment_requires_every_verified_value(self):
        with mock.patch.object(cfg, "TEST_MODE", True):
            env = dict(cfg.VERIFIED_TEST_TARGETS)
            with mock.patch.dict(os.environ, env, clear=True):
                cfg.validate_test_environment()
                os.environ["PUMA_PO_DRIVE_FOLDER_ID"] = "wrong-folder"
                with self.assertRaises(RuntimeError):
                    cfg.validate_test_environment()

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
