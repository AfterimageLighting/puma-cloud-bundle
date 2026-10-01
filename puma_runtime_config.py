"""Runtime configuration helpers for PUMA Cloud processors.

Production behavior remains backward-compatible where a production default is
supplied. When PUMA_TEST_MODE=1, configuration is fail-closed and all known
routing targets must equal the independently verified PUMA TEST resources.
"""

import os

LIVE_PUMA_SPREADSHEET_ID = "1pwVlYSGVjyTCLt4GT7xU2TCnxfdJuxAbp_jU6Snisls"

VERIFIED_TEST_TARGETS = {
    "PUMA_SPREADSHEET_ID": "1wjGB4dUTbBUiWVC7Kh0huVw8wM_ppmjTgktLw6ElVf4",
    "PUMA_OKD_LABEL_ID": "Label_1",
    "PUMA_RFA_LABEL_NAME": "PUMA TEST/RFA",
    "PUMA_RFA_PROCESSED_LABEL_NAME": "PUMA TEST/RFA Processed",
    "PUMA_RFPO_LABEL_ID": "Label_3",
    "PUMA_PO_LABEL_NAME": "PUMA TEST/PO",
    "PUMA_RR_LABEL_NAME": "PUMA TEST/RR",
    "PUMA_RFPS_LABEL_ID": "Label_6",
    "PUMA_DR_LABEL_NAME": "PUMA TEST/DR",
    "PUMA_PO_DRIVE_FOLDER_ID": "1EeexxItqTX896tq64lf1-8lBz2bqCBmH",
    "PUMA_RR_BASE_FOLDER_ID": "1WMpQwDwGGVq8R1AYxvJPAP8gOrgmP9Bg",
    "PUMA_DR_BASE_FOLDER_ID": "12hNy09LDDFAjyDl4q97GTTsCysyvSgJf",
    "PUMA_ALLOWED_STEPS": "OKD,RFA,RFPO,PO,RR,RFPS,DR",
    "PUMA_DISABLED_STEPS": "",
    "PUMA_ARGS": "--nodebug",
    "PUMA_EXPECTED_GMAIL_ACCOUNT": "adrian@afterimagelighting.com",
}


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


TEST_MODE = _truthy(os.getenv("PUMA_TEST_MODE", ""))


def _assert_verified_test_target(name: str, value: str) -> str:
    if TEST_MODE and name in VERIFIED_TEST_TARGETS:
        expected = VERIFIED_TEST_TARGETS[name]
        if value != expected:
            raise RuntimeError(
                f"{name} does not match the verified PUMA TEST target"
            )
    return value


def test_safe_env(name: str, production_default: str = "") -> str:
    value = os.getenv(name, "").strip()

    if TEST_MODE:
        if not value:
            raise RuntimeError(
                f"{name} must be explicitly set when PUMA_TEST_MODE=1"
            )
        if production_default and value == production_default:
            raise RuntimeError(
                f"{name} still points to the production target while "
                "PUMA_TEST_MODE=1"
            )
        return _assert_verified_test_target(name, value)

    return value or production_default


def required_env(name: str, forbidden_in_test: str = "") -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    if TEST_MODE and forbidden_in_test and value == forbidden_in_test:
        raise RuntimeError(
            f"{name} still points to the production target while "
            "PUMA_TEST_MODE=1"
        )
    return _assert_verified_test_target(name, value)


def validate_test_environment() -> None:
    """Fail before any processor starts if the TEST environment drifted."""
    if not TEST_MODE:
        return
    for name, expected in VERIFIED_TEST_TARGETS.items():
        actual = os.getenv(name, "").strip()
        if actual != expected:
            raise RuntimeError(
                f"{name} must equal the verified PUMA TEST value"
            )
