"""Compatibility entrypoint.

The legacy orchestrator has been retired. All executions delegate to the
hardened PUMA_Master_v2 implementation.
"""

from PUMA_Master_v2 import main


if __name__ == "__main__":
    main()
