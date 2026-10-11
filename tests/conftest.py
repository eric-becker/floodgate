"""Shared pytest configuration and markers.

Test tiers:
  (default)   Unit tests — no external dependencies.
  smoke       Container smoke test — builds and starts the image, checks /health.
              Requires Docker. Run with: pytest -m smoke
  The Docker Compose integration harness is not a pytest tier; run it with
  ./scripts/run-integration.sh.

In CI, tiers are run as separate jobs so failures are clearly attributed.
"""

import sys
from pathlib import Path

# Make the generated meshtastic protobuf bindings importable when present.
# Tests that need them call pytest.importorskip("meshtastic") and skip
# gracefully when the bindings aren't generated.
_generated = Path(__file__).resolve().parent.parent / "generated"
if _generated.is_dir() and str(_generated) not in sys.path:
    sys.path.insert(0, str(_generated))


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires Docker daemon")
    config.addinivalue_line("markers", "smoke: build and start the container, requires Docker")
