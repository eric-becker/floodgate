"""Tests for JSON and text formatter setup."""

import json
import logging

from floodgate.log_setup import StructuredTextFormatter, build_formatter


class TestBuildFormatter:

    def test_text_formatter_is_structured(self):
        fmt = build_formatter("text")
        assert isinstance(fmt, StructuredTextFormatter)

    def test_json_formatter_emits_valid_json(self):
        fmt = build_formatter("json")
        record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
        output = fmt.format(record)
        data = json.loads(output)
        assert data["message"] == "hello"
        assert "level" in data
        assert "levelname" not in data

    def test_json_formatter_includes_extra_fields(self):
        fmt = build_formatter("json")
        record = logging.LogRecord("test", logging.INFO, "", 0, "zerohop", (), None)
        record.event = "message"
        record.outcome = "zerohop"
        record.channel = "LongFast"
        record.hop_limit = 3
        record.hop_start = 5
        output = fmt.format(record)
        data = json.loads(output)
        assert data["outcome"] == "zerohop"
        assert data["channel"] == "LongFast"
        assert data["hop_limit"] == 3
        assert data["hop_start"] == 5

    def test_json_message_is_just_outcome_tag(self):
        fmt = build_formatter("json")
        record = logging.LogRecord("test", logging.INFO, "", 0, "zerohop", (), None)
        record.event = "message"
        record.outcome = "zerohop"
        record.topic = "msh/US/2/e/LongFast/!1234"
        record.channel = "LongFast"
        data = json.loads(fmt.format(record))
        assert data["message"] == "zerohop"

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
        assert "asctime" not in data


class TestStructuredTextFormatter:

    def _format(self, msg, **extras):
        fmt = StructuredTextFormatter()
        record = logging.LogRecord("test", logging.INFO, "", 0, msg, (), None)
        for k, v in extras.items():
            setattr(record, k, v)
        return fmt.format(record)

    def test_message_event_renders_outcome_tag(self):
        output = self._format(
            "zerohop", event="message", outcome="zerohop",
            topic="msh/US/2/e/LongFast/!1234", channel="LongFast",
            encoding="e", hop_limit=3, hop_start=3,
        )
        assert "[ZEROHOP]" in output
        assert "topic=msh/US/2/e/LongFast/!1234" in output
        assert "channel=LongFast" in output
        assert "hop_limit=3" in output
        assert "hop_start=3" in output

    def test_stats_event_renders_stats_tag(self):
        output = self._format(
            "stats", event="stats", interval_s=60,
            zerohop=5, passthru=1, noop=0, skipped=10, errors=0, total=16,
        )
        assert "[STATS]" in output
        assert "zerohop=5" in output
        assert "total=16" in output

    def test_plain_message_passes_through(self):
        output = self._format("ExHook gRPC server listening on port 9000")
        assert "ExHook gRPC server listening on port 9000" in output
        assert "[ZEROHOP]" not in output
        assert "[STATS]" not in output

    def test_none_fields_omitted(self):
        output = self._format(
            "passthru", event="message", outcome="passthru",
            topic="msh/US/2/e/LongFast/!1234", channel="LongFast",
            encoding="e", relay=None, via_mqtt=None,
        )
        assert "relay=" not in output
        assert "via_mqtt=" not in output
