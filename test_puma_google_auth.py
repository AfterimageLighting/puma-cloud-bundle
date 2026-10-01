import os
import unittest
from unittest import mock

import puma_google_auth as auth


class GoogleAuthSafetyTests(unittest.TestCase):
    def test_test_mode_never_starts_browser_flow(self):
        with mock.patch.object(auth, "TEST_MODE", True):
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(auth.os.path, "exists", return_value=False):
                    with mock.patch.object(auth.InstalledAppFlow, "from_client_secrets_file") as flow:
                        with self.assertRaises(RuntimeError):
                            auth.load_google_user_credentials(["scope"])
                        flow.assert_not_called()

    def test_cloud_runtime_never_starts_browser_flow(self):
        with mock.patch.object(auth, "TEST_MODE", False):
            with mock.patch.dict(os.environ, {"K_SERVICE": "puma-test"}, clear=True):
                with mock.patch.object(auth.os.path, "exists", return_value=False):
                    with mock.patch.object(auth.InstalledAppFlow, "from_client_secrets_file") as flow:
                        with self.assertRaises(RuntimeError):
                            auth.load_google_user_credentials(["scope"])
                        flow.assert_not_called()

    def test_env_refresh_credentials_are_supported(self):
        fake = mock.Mock()
        fake.refresh = mock.Mock()
        with mock.patch.object(auth, "TEST_MODE", True):
            with mock.patch.dict(
                os.environ,
                {
                    "GMAIL_CLIENT_ID": "client",
                    "GMAIL_CLIENT_SECRET": "secret",
                    "GMAIL_REFRESH_TOKEN": "refresh",
                },
                clear=True,
            ):
                with mock.patch.object(auth, "Credentials", return_value=fake) as creds_cls:
                    result = auth.load_google_user_credentials(["scope"])
        self.assertIs(result, fake)
        fake.refresh.assert_called_once()
        creds_cls.assert_called_once()

    def test_refreshable_token_file_is_supported_without_browser(self):
        fake = mock.Mock()
        fake.valid = False
        fake.expired = True
        fake.refresh_token = "refresh"
        fake.to_json.return_value = "{}"

        mocked_open = mock.mock_open()
        with mock.patch.object(auth, "TEST_MODE", True):
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(auth.os.path, "exists", side_effect=lambda p: p == "token.json"):
                    with mock.patch.object(
                        auth.Credentials,
                        "from_authorized_user_file",
                        return_value=fake,
                    ):
                        with mock.patch.object(auth.InstalledAppFlow, "from_client_secrets_file") as flow:
                            with mock.patch("builtins.open", mocked_open):
                                result = auth.load_google_user_credentials(["scope"])
        self.assertIs(result, fake)
        fake.refresh.assert_called_once()
        flow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
