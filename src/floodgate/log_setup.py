"""Logging formatter setup — text (default) or JSON (for Loki/Grafana)."""

import logging
import time

# Fields rendered by StructuredTextFormatter, in display order.
# `portnum` appears on dropped events (and is harmless when absent on others).
_MESSAGE_FIELDS = (
    "topic",
    "channel",
    "encoding",
    "portnum",
    "id",
    "from",
    "to",
    "hop_limit",
    "hop_start",
    "relay",
    "via_mqtt",
    "bytes",
)
_STATS_FIELDS = (
    "interval_s",
    "zerohop",
    "passthru",
    "noop",
    "dropped",
    "skipped",
    "errors",
    "total",
)

# Outcome tags → display labels for text mode
_OUTCOME_TAGS = {
    "zerohop": "[ZEROHOP]",
    "passthru": "[PASSTHRU]",
    "noop": "[NOOP]",
    "dropped": "[DROPPED]",
    "warn": "[WARN]",
}


def _escape(value) -> str:
    """Render a field value on one line.

    Topics and channel names are publisher-controlled, so a newline or ANSI
    escape could otherwise forge a line that looks like a floodgate outcome.
    Control characters are written as Python escapes; printable non-ASCII
    (channel names in any script) is kept as-is.
    """
    text = str(value)
    if text.isprintable():
        return text
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


class StructuredTextFormatter(logging.Formatter):
    """Renders structured extra={} fields as key=value pairs in text mode."""

    def __init__(self):
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record):
        event = getattr(record, "event", None)
        if event == "message":
            record.msg = self._format_message(record)
            record.args = None
        elif event == "stats":
            record.msg = self._format_stats(record)
            record.args = None
        return super().format(record)

    def _format_message(self, record):
        outcome = getattr(record, "outcome", "")
        tag = _OUTCOME_TAGS.get(outcome, f"[{outcome.upper()}]")
        parts = [tag]
        for key in _MESSAGE_FIELDS:
            val = getattr(record, key, None)
            if val is not None:
                parts.append(f"{key}={_escape(val)}")
        return " ".join(parts)

    def _format_stats(self, record):
        parts = ["[STATS]"]
        for key in _STATS_FIELDS:
            val = getattr(record, key, None)
            if val is not None:
                parts.append(f"{key}={val}")
        return " ".join(parts)


def build_formatter(log_format: str) -> logging.Formatter:
    """Return a Formatter for the requested format ('text' or 'json')."""
    if log_format == "json":
        try:
            from pythonjsonlogger.json import JsonFormatter
        except ImportError:
            from pythonjsonlogger.jsonlogger import JsonFormatter  # type: ignore[no-redef]

        formatter = JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"levelname": "level", "asctime": "timestamp"},
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        # The datefmt promises UTC ("Z"); make it true on non-UTC hosts.
        formatter.converter = time.gmtime
        return formatter
    return StructuredTextFormatter()


def configure_logging(level: int, log_format: str) -> None:
    """Install root handler with correct formatter, replacing any prior handler."""
    handler = logging.StreamHandler()
    handler.setFormatter(build_formatter(log_format))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
