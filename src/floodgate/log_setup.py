"""Logging formatter setup — text (default) or JSON (for Loki/Grafana)."""

import logging


def build_formatter(log_format: str) -> logging.Formatter:
    """Return a Formatter for the requested format ('text' or 'json')."""
    if log_format == "json":
        try:
            from pythonjsonlogger.json import JsonFormatter
        except ImportError:
            from pythonjsonlogger.jsonlogger import JsonFormatter  # type: ignore[no-redef]

        return JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"levelname": "level", "asctime": "timestamp"},
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    return logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def configure_logging(level: int, log_format: str) -> None:
    """Install root handler with correct formatter, replacing any prior handler."""
    handler = logging.StreamHandler()
    handler.setFormatter(build_formatter(log_format))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
