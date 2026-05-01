"""Tests for JSON and text formatter setup."""

import json
import logging
import re

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


class TestJsonFormatterSnapshots:
    """Snapshot tests validating the exact JSON output structure."""

    def _format_json(self, msg, level=logging.INFO, **extras):
        fmt = build_formatter("json")
        record = logging.LogRecord(
            "floodgate.zerohop", level, "", 0, msg, (), None,
        )
        record.created = 1743508801.0
        for k, v in extras.items():
            setattr(record, k, v)
        return json.loads(fmt.format(record))

    def test_zerohop_message_fields(self):
        data = self._format_json(
            "zerohop", event="message", outcome="zerohop",
            topic="msh/US/2/e/LongFast/!1234", channel="LongFast",
            encoding="e", id="0x12345678", hop_limit=0, hop_start=3,
            **{"from": "!abcd1234", "to": "!ffffffff"},
        )
        assert data["message"] == "zerohop"
        assert data["event"] == "message"
        assert data["outcome"] == "zerohop"
        assert data["topic"] == "msh/US/2/e/LongFast/!1234"
        assert data["channel"] == "LongFast"
        assert data["encoding"] == "e"
        assert data["id"] == "0x12345678"
        assert data["from"] == "!abcd1234"
        assert data["to"] == "!ffffffff"
        assert data["hop_limit"] == 0
        assert data["hop_start"] == 3
        assert data["level"] == "INFO"
        assert "levelname" not in data
        assert "timestamp" in data
        assert "asctime" not in data
        assert data["name"] == "floodgate.zerohop"

    def test_noop_message_fields(self):
        data = self._format_json(
            "noop", event="message", outcome="noop",
            topic="msh/US/2/e/LongFast/!5678", channel="LongFast",
            encoding="e", id="0xdeadbeef", hop_limit=0, hop_start=3,
            **{"from": "!abcd1234", "to": "!ffffffff"},
        )
        assert data["outcome"] == "noop"
        assert data["hop_limit"] == 0
        assert data["hop_start"] == 3

    def test_passthru_message_fields(self):
        data = self._format_json(
            "passthru", event="message", outcome="passthru",
            topic="msh/US/2/e/MyChannel/!9999", channel="MyChannel",
            encoding="e",
        )
        assert data["outcome"] == "passthru"
        assert "hop_limit" not in data
        assert "hop_start" not in data

    def test_warn_message_fields(self):
        data = self._format_json(
            "warn", level=logging.WARNING, event="message", outcome="warn",
            topic="msh/US/2/e/LongFast/!1234", channel="LongFast",
            encoding="e", bytes=42,
        )
        assert data["level"] == "WARNING"
        assert data["bytes"] == 42
        assert data["outcome"] == "warn"

    def test_stats_event_fields(self):
        data = self._format_json(
            "stats", event="stats",
            interval_s=60, zerohop=142, passthru=1, noop=0,
            skipped=1050, errors=0, total=1193,
        )
        assert data["event"] == "stats"
        assert data["interval_s"] == 60
        assert data["zerohop"] == 142
        assert data["passthru"] == 1
        assert data["noop"] == 0
        assert data["skipped"] == 1050
        assert data["errors"] == 0
        assert data["total"] == 1193

    def test_timestamp_format_iso8601(self):
        data = self._format_json("ts check")
        ts = data["timestamp"]
        assert ts.endswith("Z")
        assert "T" in ts
        assert len(ts) == 20

    def test_none_fields_present_as_null_in_json(self):
        data = self._format_json(
            "passthru", event="message", outcome="passthru",
            topic="msh/US/2/e/LongFast/!1234", channel="LongFast",
            relay=None, via_mqtt=None,
        )
        # python-json-logger includes None values as JSON null
        assert data["relay"] is None
        assert data["via_mqtt"] is None

    def test_relay_field_when_present(self):
        data = self._format_json(
            "zerohop", event="message", outcome="zerohop",
            topic="msh/US/2/e/LongFast/!1234", channel="LongFast",
            relay="ab", via_mqtt=True,
        )
        assert data["relay"] == "ab"
        assert data["via_mqtt"] is True


class TestTextFormatterSnapshots:
    """Snapshot tests validating exact text output structure and field order."""

    def _format_text(self, msg, level=logging.INFO, **extras):
        fmt = StructuredTextFormatter()
        record = logging.LogRecord(
            "floodgate.zerohop", level, "", 0, msg, (), None,
        )
        for k, v in extras.items():
            setattr(record, k, v)
        return fmt.format(record)

    def test_zerohop_exact_field_order(self):
        output = self._format_text(
            "zerohop", event="message", outcome="zerohop",
            topic="msh/US/2/e/LongFast/!1234", channel="LongFast",
            encoding="e", id="0x12345678", hop_limit=0, hop_start=3,
            **{"from": "!abcd1234", "to": "!ffffffff"},
        )
        expected_tail = (
            "[ZEROHOP] topic=msh/US/2/e/LongFast/!1234 channel=LongFast"
            " encoding=e id=0x12345678 from=!abcd1234 to=!ffffffff"
            " hop_limit=0 hop_start=3"
        )
        assert output.endswith(expected_tail)
        assert "[floodgate.zerohop]" in output
        assert "INFO" in output

    def test_noop_exact_output(self):
        output = self._format_text(
            "noop", event="message", outcome="noop",
            topic="msh/US/2/e/LongFast/!5678", channel="LongFast",
            encoding="e", id="0xdeadbeef", hop_limit=0, hop_start=3,
            **{"from": "!abcd1234", "to": "!ffffffff"},
        )
        expected_tail = (
            "[NOOP] topic=msh/US/2/e/LongFast/!5678 channel=LongFast"
            " encoding=e id=0xdeadbeef from=!abcd1234 to=!ffffffff"
            " hop_limit=0 hop_start=3"
        )
        assert output.endswith(expected_tail)

    def test_passthru_no_hop_fields(self):
        output = self._format_text(
            "passthru", event="message", outcome="passthru",
            topic="msh/US/2/e/MyChannel/!9999", channel="MyChannel",
            encoding="e",
        )
        assert "[PASSTHRU]" in output
        assert "hop_limit=" not in output
        assert "hop_start=" not in output

    def test_warn_exact_output(self):
        output = self._format_text(
            "warn", level=logging.WARNING, event="message", outcome="warn",
            topic="msh/US/2/e/LongFast/!1234", channel="LongFast",
            encoding="e", bytes=42,
        )
        assert "[WARN]" in output
        assert "WARNING" in output
        assert "topic=" in output
        # bytes is not in _MESSAGE_FIELDS so it won't appear in text output
        assert "bytes=" not in output

    def test_stats_exact_field_order(self):
        output = self._format_text(
            "stats", event="stats",
            interval_s=60, zerohop=142, passthru=1, noop=0,
            skipped=1050, errors=0, total=1193,
        )
        expected_tail = (
            "[STATS] interval_s=60 zerohop=142 passthru=1"
            " noop=0 skipped=1050 errors=0 total=1193"
        )
        assert output.endswith(expected_tail)

    def test_relay_and_via_mqtt_included(self):
        output = self._format_text(
            "zerohop", event="message", outcome="zerohop",
            topic="msh/US/2/e/LongFast/!1234", channel="LongFast",
            encoding="e", relay="ab", via_mqtt=True,
        )
        assert "relay=ab" in output
        assert "via_mqtt=True" in output

    def test_none_fields_omitted_from_text(self):
        output = self._format_text(
            "passthru", event="message", outcome="passthru",
            topic="msh/US/2/e/LongFast/!1234", channel="LongFast",
            encoding="e", relay=None, via_mqtt=None,
        )
        assert "relay=" not in output
        assert "via_mqtt=" not in output

    def test_timestamp_format(self):
        output = self._format_text("hello")
        assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", output)
