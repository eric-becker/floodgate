"""Shared pytest configuration and markers.

Test tiers:
  (default)   Unit tests — no external dependencies, always run.
  integration Docker-based tests — require Docker daemon, skipped by default.
              Run with: pytest -m integration
  smoke       Container/deployment smoke tests — build + start image, run /health.
              Run with: pytest -m smoke

In CI, tiers are run as separate jobs so failures are clearly attributed.
"""

import sys
from pathlib import Path

# Make the generated meshtastic protobuf bindings importable when present.
# Tests that need them call pytest.importorskip("meshtastic") and skip
# gracefully when the bindings aren't generated (e.g. CI's mock-only path).
_generated = Path(__file__).resolve().parent.parent / "generated"
if _generated.is_dir() and str(_generated) not in sys.path:
    sys.path.insert(0, str(_generated))


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires Docker daemon")
    config.addinivalue_line("markers", "smoke: build and start the container, requires Docker")
