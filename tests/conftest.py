"""Shared pytest configuration and markers.

Test tiers:
  (default)   Unit tests — no external dependencies, always run.
  integration Docker-based tests — require Docker daemon, skipped by default.
              Run with: pytest -m integration
  smoke       Container/deployment smoke tests — build + start image, run /health.
              Run with: pytest -m smoke

In CI, tiers are run as separate jobs so failures are clearly attributed.
"""



def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires Docker daemon")
    config.addinivalue_line("markers", "smoke: build and start the container, requires Docker")
