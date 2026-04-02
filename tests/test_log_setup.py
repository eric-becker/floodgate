"""Tests for JSON and text formatter setup."""

import json
import logging

from floodgate.log_setup import build_formatter


class TestBuildFormatter:

    def test_text_formatter_is_standard(self):
        fmt = build_formatter("text")
        assert isinstance(fmt, logging.Formatter)
        record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
        output = fmt.format(record)
        assert "hello" in output
        assert "INFO" in output

    def test_json_formatter_emits_valid_json(self):
        fmt = build_formatter("json")
        record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
        output = fmt.format(record)
        data = json.loads(output)
        assert data["message"] == "hello"
        assert "level" in data
        assert "levelname" not in data   # must be renamed to "level"

    def test_json_formatter_includes_extra_fields(self):
        fmt = build_formatter("json")
        record = logging.LogRecord("test", logging.INFO, "", 0, "zerohop", (), None)
        record.outcome  = "zerohop"
        record.channel  = "LongFast"
        record.hop_from = 3
        record.hop_to   = 0
        output = fmt.format(record)
        data = json.loads(output)
        assert data["outcome"]  == "zerohop"
        assert data["channel"]  == "LongFast"
        assert data["hop_from"] == 3
        assert data["hop_to"]   == 0

    def test_json_formatter_level_value(self):
        fmt = build_formatter("json")
        record = logging.LogRecord("test", logging.WARNING, "", 0, "oops", (), None)
        data = json.loads(fmt.format(record))
        assert data["level"] in ("WARNING", "WARN")

    def test_json_formatter_has_timestamp(self):
        fmt = build_formatter("json")
        record = logging.LogRecord("test", logging.INFO, "", 0, "ts", (), None)
        data = json.loads(fmt.format(record))
        assert "timestamp" in data
        assert "asctime" not in data     # must be renamed to "timestamp"
