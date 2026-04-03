"""Container smoke tests — build the image and verify it starts correctly.

These tests require a Docker daemon and are skipped in unit-test runs.
Run explicitly with:  pytest -m smoke -v

In CI this runs as a separate job (see .github/workflows/ci.yml) after the
unit tests pass, so a container startup failure is clearly distinguished from
a logic failure.

What is tested:
  - The Dockerfile builds without error
  - The container starts and the health endpoint returns HTTP 200
  - The /health response body is valid JSON with {"status": "ok"}
  - Graceful shutdown: the container stops cleanly within a timeout
"""

import json
import subprocess
import time
import urllib.error
import urllib.request

import pytest

IMAGE_TAG = "floodgate-smoke-test:ci"
HEALTH_PORT = 8089   # high-numbered to avoid collisions with real deployments
STARTUP_TIMEOUT = 30  # seconds to wait for /health to become available


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_for_health(url: str, timeout: int = STARTUP_TIMEOUT) -> tuple[int, bytes]:
    """Poll url until it returns HTTP 200 or timeout expires."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status, resp.read()
        except Exception as exc:
            last_exc = exc
            time.sleep(1)
    raise TimeoutError(
        f"/health did not respond within {timeout}s: {last_exc}"
    )


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Session-scoped fixtures: build once, reuse across tests in this file
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def built_image(tmp_path_factory):
    """Build the Docker image once for the session and remove it on teardown."""
    result = _docker("build", "-t", IMAGE_TAG, ".")
    if result.returncode != 0:
        pytest.fail(f"docker build failed:\n{result.stderr}")
    yield IMAGE_TAG
    _docker("rmi", "-f", IMAGE_TAG)


@pytest.fixture(scope="session")
def running_container(built_image, tmp_path_factory):
    """Start the container and yield its container ID; stop + remove on teardown."""
    result = _docker(
        "run", "-d",
        "--name", "floodgate-smoke",
        "-p", f"{HEALTH_PORT}:8080",
        built_image,
    )
    if result.returncode != 0:
        pytest.fail(f"docker run failed:\n{result.stderr}")
    container_id = result.stdout.strip()

    # Wait for the container's health check to declare the process live
    try:
        _wait_for_health(f"http://localhost:{HEALTH_PORT}/health")
    except TimeoutError as exc:
        # Capture container logs to aid diagnosis before failing
        logs = _docker("logs", container_id).stdout
        pytest.fail(f"{exc}\nContainer logs:\n{logs}")

    yield container_id

    _docker("stop", container_id)
    _docker("rm", "-f", container_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestContainerSmoke:

    def test_health_returns_200(self, running_container):
        status, _ = _wait_for_health(f"http://localhost:{HEALTH_PORT}/health")
        assert status == 200

    def test_health_body_is_valid_json(self, running_container):
        _, body = _wait_for_health(f"http://localhost:{HEALTH_PORT}/health")
        data = json.loads(body)
        assert data["status"] == "ok"

    def test_health_body_has_stats(self, running_container):
        _, body = _wait_for_health(f"http://localhost:{HEALTH_PORT}/health")
        data = json.loads(body)
        stats = data["stats"]
        for key in ("zerohop", "passthru", "noop", "skipped", "errors", "total"):
            assert key in stats, f"missing stats key: {key}"

    def test_unknown_path_returns_404(self, running_container):
        try:
            with urllib.request.urlopen(
                f"http://localhost:{HEALTH_PORT}/notfound", timeout=5
            ) as resp:
                pytest.fail(f"Expected 404 but got {resp.status}")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404

    def test_container_is_running(self, running_container):
        result = _docker("inspect", "--format", "{{.State.Status}}", running_container)
        assert result.stdout.strip() == "running"

    def test_container_runs_as_nonroot(self, running_container):
        result = _docker("exec", running_container, "id", "-u")
        # Dockerfile uses 'nobody' (uid 65534); any uid != 0 is acceptable
        assert result.returncode == 0
        uid = result.stdout.strip()
        assert uid != "0", f"Container is running as root (uid={uid})"
