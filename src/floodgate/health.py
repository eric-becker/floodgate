"""Minimal HTTP health check server — no external dependencies."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from .antiflood import stats as antiflood_stats

logger = logging.getLogger(__name__)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({
                "status": "ok",
                "stats": antiflood_stats.snapshot(),
            }).encode()
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


def start_health_server(port: int) -> threading.Thread:
    """Start the HTTP health server in a daemon thread. Returns immediately."""
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="health-server",
    )
    thread.start()
    logger.info("Health check listening on port %d  GET /health", port)
    return thread
