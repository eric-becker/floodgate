"""Tests for the HTTP health check server."""

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

from floodgate.health import _HealthHandler, start_health_server
from floodgate.zerohop import stats as packet_stats

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _start_test_server():
    """Start an HTTPServer on an OS-assigned port. Returns (server, url)."""
    server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def _get(url):
    """Perform GET and return (status_code, body_bytes). Handles 404 via urllib."""
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHealthEndpoint:

    def setup_method(self):
        # Reset both rolling and lifetime counters before each test
        with packet_stats._lock:
            packet_stats.zerohop = 0
            packet_stats.passthru = 0
            packet_stats.noop = 0
            packet_stats.skipped = 0
            packet_stats.errors = 0
            for k in packet_stats._lifetime:
                packet_stats._lifetime[k] = 0

    def test_health_returns_200(self):
        server, base = _start_test_server()
        try:
            status, _ = _get(f"{base}/health")
            assert status == 200
        finally:
            server.shutdown()

    def test_health_content_type_is_json(self):
        server, base = _start_test_server()
        try:
            with urllib.request.urlopen(f"{base}/health") as resp:
                assert "application/json" in resp.headers.get("Content-Type", "")
        finally:
            server.shutdown()

    def test_health_body_has_status_ok(self):
        server, base = _start_test_server()
        try:
            _, body = _get(f"{base}/health")
            data = json.loads(body)
            assert data["status"] == "ok"
        finally:
            server.shutdown()

    def test_health_body_has_stats_keys(self):
        server, base = _start_test_server()
        try:
            _, body = _get(f"{base}/health")
            data = json.loads(body)
            stats = data["stats"]
            for key in ("zerohop", "passthru", "noop", "skipped", "errors", "total"):
                assert key in stats, f"missing key: {key}"
        finally:
            server.shutdown()

    def test_health_stats_reflect_lifetime_counters(self):
        for _ in range(5):
            packet_stats.inc("zerohop")
        for _ in range(2):
            packet_stats.inc("passthru")
        packet_stats.inc("noop")

        server, base = _start_test_server()
        try:
            _, body = _get(f"{base}/health")
            stats = json.loads(body)["stats"]
            assert stats["zerohop"] == 5
            assert stats["passthru"] == 2
            assert stats["noop"] == 1
            assert stats["total"] == 8
        finally:
            server.shutdown()

    def test_health_stats_survive_reset(self):
        """Lifetime counters persist after stats reporter reset()."""
        for _ in range(3):
            packet_stats.inc("zerohop")
        packet_stats.inc("passthru")

        # Simulate stats reporter draining the rolling window
        packet_stats.reset()

        server, base = _start_test_server()
        try:
            _, body = _get(f"{base}/health")
            stats = json.loads(body)["stats"]
            assert stats["zerohop"] == 3
            assert stats["passthru"] == 1
            assert stats["total"] == 4
        finally:
            server.shutdown()

    def test_unknown_path_returns_404(self):
        server, base = _start_test_server()
        try:
            status, _ = _get(f"{base}/notfound")
            assert status == 404
        finally:
            server.shutdown()

    def test_root_returns_404(self):
        server, base = _start_test_server()
        try:
            status, _ = _get(f"{base}/")
            assert status == 404
        finally:
            server.shutdown()


class TestStartHealthServer:

    def test_returns_daemon_thread(self):
        server_thread = start_health_server(0)
        # start_health_server binds to 0.0.0.0:port — just verify the thread
        assert isinstance(server_thread, threading.Thread)
        assert server_thread.daemon is True
        assert server_thread.is_alive()
