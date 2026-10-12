"""Minimal HTTP health check server — no external dependencies."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from .zerohop import stats as packet_stats

logger = logging.getLogger(__name__)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps(
                {
                    "status": "ok",
                    "stats": packet_stats.snapshot(),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress per-request access logs


def start_health_server(port: int, host: str = "0.0.0.0") -> HTTPServer:
    """Start the HTTP health server in a daemon thread and return the server.

    Returns immediately. Call `shutdown()` then `server_close()` on the result
    to stop it. Port 0 binds an ephemeral port; read it from `server_address`.
    """
    server = HTTPServer((host, port), _HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="health-server",
    )
    thread.start()
    logger.info("Health check listening on port %d  GET /health", server.server_address[1])
    return server
