"""Runtime configuration helpers for PUMA Cloud processors.

Production behavior remains backward-compatible where a production default is
supplied. When PUMA_TEST_MODE=1, however, configuration is fail-closed:
required test values must be explicitly supplied and may not equal known
production targets.
"""

import os

LIVE_PUMA_SPREADSHEET_ID = "1pwVlYSGVjyTCLt4GT7xU2TCnxfdJuxAbp_jU6Snisls"

def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

TEST_MODE = _truthy(os.getenv("PUMA_TEST_MODE", ""))

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
        return value

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
    return value
