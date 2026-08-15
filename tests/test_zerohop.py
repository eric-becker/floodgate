"""Tests for the per-message processing pipeline (drop, zerohop, logging)."""

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from floodgate.zerohop import (
    ACTION_DROP,
    ACTION_MODIFY,
    ACTION_PASSTHRU,
    AntifloodStats,
    _fmt_node,
    _peek_meta,
    parse_meshtastic_topic,
    process_message,
    zerohop_json,
    zerohop_protobuf,
)

# Directory of real-world Meshtastic payloads captured from gateways.
# Each JSON file is the raw form exactly as published on /json/ topics; each
# .bin under protobuf/ is a captured ServiceEnvelope. Drop new samples in to
# extend coverage — they're picked up automatically by the parametrized
# smoke tests, and individual tests can load them by filename for detailed
# assertions.
PAYLOADS_DIR          = Path(__file__).parent / "payloads"
PROTOBUF_PAYLOADS_DIR = PAYLOADS_DIR / "protobuf"


def _list_json_payloads():
    return sorted(PAYLOADS_DIR.glob("*.json"))


def _list_protobuf_payloads():
    if not PROTOBUF_PAYLOADS_DIR.exists():
        return []
    return sorted(PROTOBUF_PAYLOADS_DIR.glob("*.bin"))


def _load_json_payload(name):
    return (PAYLOADS_DIR / name).read_bytes()


def _make_config(*, zerohop_enabled=True, zerohop_channels=(),
                 drop_enabled=False, drop_channels=None, drop_portnums=()):
    """Build a fully-resolved config dict (with the precomputed sets that
    load_config would otherwise produce). Tests skip load_config to keep
    individual cases tight and explicit."""
    zerohop_set = set(zerohop_channels)
    return {
        "zerohop_enabled":         zerohop_enabled,
        "zerohop_channels":        list(zerohop_channels),
        "_zerohop_channels_set":   zerohop_set,
        "drop_enabled":            drop_enabled,
        "drop_channels":           drop_channels,
        "_drop_channels_set":      set(drop_channels) if drop_channels else None,
        "drop_portnums":           list(drop_portnums),
        "_drop_portnums_set":      set(drop_portnums),
    }


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

class TestFmtNode:

    def test_int_node_id(self):
        assert _fmt_node(0xDEADBEEF) == "!deadbeef"

    def test_broadcast(self):
        assert _fmt_node(0xFFFFFFFF) == "!ffffffff"

    def test_none(self):
        assert _fmt_node(None) == "?"

    def test_string_passthrough(self):
        assert _fmt_node("!f6acb04f") == "!f6acb04f"


class TestParseMeshtasticTopic:

    def test_protobuf_topic_returns_channel_and_encoding(self):
        assert parse_meshtastic_topic("msh/US/2/e/LongFast/!12345678") == ("LongFast", "e")

    def test_json_topic_returns_channel_and_encoding(self):
        assert parse_meshtastic_topic("msh/US/2/json/LongFast/!12345678") == ("LongFast", "json")

    def test_non_meshtastic_topic(self):
        assert parse_meshtastic_topic("homeassistant/sensor/temp") is None

    def test_short_topic(self):
        assert parse_meshtastic_topic("msh/US") is None

    def test_empty_topic(self):
        assert parse_meshtastic_topic("") is None

    def test_private_channel(self):
        result = parse_meshtastic_topic("msh/US/2/e/MyPrivateChannel/!aabbccdd")
        assert result == ("MyPrivateChannel", "e")

    def test_deep_topic_three_prefix_parts(self):
        # msh/{country}/{region}/2/e/LongFast/!nodeId — 3-part prefix
        assert parse_meshtastic_topic("msh/US/CA/2/e/LongFast/!16cec9ac") == ("LongFast", "e")

    def test_deep_topic_four_prefix_parts(self):
        # msh/{country}/{region}/{area}/2/e/LongFast/!nodeId — 4-part prefix
        assert parse_meshtastic_topic("msh/US/CA/BAY/2/e/LongFast/!087a5a9c") == ("LongFast", "e")

    def test_json_encoding_deep(self):
        assert parse_meshtastic_topic("msh/US/CA/2/json/LongFast/!deadbeef") == ("LongFast", "json")

    def test_stat_topic_not_a_packet(self):
        # Status topics without !nodeId suffix
        assert parse_meshtastic_topic("msh/US/CA") is None

    def test_no_nodeid_suffix(self):
        # msh/ prefix but no !nodeId at the end
        assert parse_meshtastic_topic("msh/US/CA/stat/somenode") is None

    def test_map_report_topic(self):
        # Map report published directly to region root — not a packet
        assert parse_meshtastic_topic("msh/US/CA/map") is None

    def test_unknown_encoding_returns_none(self):
        # Unknown encoding segment
        assert parse_meshtastic_topic("msh/US/2/xml/LongFast/!deadbeef") is None


# ---------------------------------------------------------------------------
# zerohop_json
# ---------------------------------------------------------------------------

class TestZerohopJson:

    def _payload(self, **kwargs):
        # Base: protobuf-style with explicit hop_limit
        data = {"from": 305419896, "to": 4294967295, "id": 12345, "hop_limit": 3,
                "hop_start": 3, "channel": 0, "type": "text"}
        data.update(kwargs)
        return json.dumps(data).encode()

    def _payload_meshtastic(self, **kwargs):
        # Realistic Meshtastic JSON — uses hops_away, no hop_limit field
        data = {"from": 3127570696, "to": 4294967295, "id": 1700391097,
                "hop_start": 5, "hops_away": 0, "channel": 1, "type": "text"}
        data.update(kwargs)
        return json.dumps(data).encode()

    def test_sets_hop_limit_to_zero_explicit(self):
        modified, old_hop, _ = zerohop_json(self._payload(hop_limit=3))
        assert old_hop == 3
        assert modified is not None
        assert json.loads(modified)["hop_limit"] == 0

    def test_hop_start_is_zeroed_when_present(self):
        """hop_start must also be zeroed; otherwise JSON consumers that
        compute hops-taken from hop_start see a misleading non-zero value
        (issue #46, JSON parity with the protobuf fix)."""
        modified, _, _ = zerohop_json(self._payload(hop_limit=3, hop_start=3))
        assert modified is not None
        data = json.loads(modified)
        assert data["hop_limit"] == 0
        assert data["hop_start"] == 0

    def test_hops_away_is_zeroed_when_present(self):
        """hops_away must be zeroed in the realistic Meshtastic JSON shape
        so consumers computing hops-taken = hop_start - hops_away get 0,
        not the original hop_start (issue #46)."""
        modified, _, _ = zerohop_json(
            self._payload_meshtastic(hop_start=5, hops_away=0)
        )
        assert modified is not None
        data = json.loads(modified)
        assert data["hop_limit"] == 0
        assert data["hop_start"] == 0
        assert data["hops_away"] == 0

    def test_uses_hops_away_when_no_hop_limit(self):
        modified, old_hop, _ = zerohop_json(self._payload_meshtastic(hop_start=5, hops_away=0))
        assert old_hop == 5   # effective: hop_start(5) - hops_away(0)
        assert modified is not None
        assert json.loads(modified)["hop_limit"] == 0

    def test_hops_away_equals_hop_start_is_noop(self):
        modified, old_hop, _ = zerohop_json(self._payload_meshtastic(hop_start=5, hops_away=5))
        assert modified is None
        assert old_hop == 0

    def test_noop_when_explicit_hop_limit_zero(self):
        modified, old_hop, _ = zerohop_json(self._payload(hop_limit=0))
        assert modified is None
        assert old_hop == 0

    def test_extracts_metadata(self):
        _, _, meta = zerohop_json(self._payload(hop_limit=5, hop_start=5, via_mqtt=True))
        assert meta["sender"]      == 305419896
        assert meta["destination"] == 4294967295
        assert meta["packet_id"]   == 12345
        assert meta["hop_start"]   == 5
        assert meta["via_mqtt"]    is True

    def test_packet_id_in_meta_is_decimal(self):
        _, _, meta = zerohop_json(self._payload_meshtastic())
        assert meta["packet_id"] == 1700391097  # decimal, not hex

    def test_parse_error_returns_none_none(self):
        modified, old_hop, _ = zerohop_json(b"not json at all")
        assert modified is None
        assert old_hop is None

    def test_string_node_ids_in_json(self):
        """JSON payloads may have string node IDs instead of integers."""
        data = {"from": "!f6acb04f", "to": "!ffffffff", "id": 12345,
                "hop_limit": 3, "hop_start": 3}
        modified, old_hop, meta = zerohop_json(json.dumps(data).encode())
        assert old_hop == 3
        assert modified is not None
        assert meta["sender"] == "!f6acb04f"

    def test_preserves_other_fields(self):
        modified, _, _ = zerohop_json(self._payload(hop_limit=3))
        data = json.loads(modified)
        assert data["type"]    == "text"
        assert data["channel"] == 0


# ---------------------------------------------------------------------------
# zerohop_protobuf — hop field zeroing (issue #46)
# ---------------------------------------------------------------------------

class TestZerohopProtobufHopFields:
    """Both hop_limit AND hop_start must be zeroed.

    Setting only hop_limit=0 (with hop_start unchanged) makes Meshtastic
    firmware compute hopsTaken = hop_start - hop_limit > 0, which then
    pads RouteDiscovery.route[] with 0xFFFFFFFF sentinels rendered as
    'Meshtastic ffff (ffff)' in the apps. See issue #46.
    """

    def test_hop_start_is_zeroed_alongside_hop_limit(self):
        pytest.importorskip("meshtastic")
        from meshtastic import mesh_pb2, mqtt_pb2, portnums_pb2

        from tests.test_portnum import _build_encrypted_envelope

        envelope_bytes = _build_encrypted_envelope(
            mesh_pb2, mqtt_pb2,
            portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            payload_bytes=b"x",
            packet_id=0x11223344,
            from_node=0xAABBCCDD,
            channel_name="LongFast",
            hop_limit=3,
            hop_start=3,
        )
        # Verify test premise: both fields are set to 3 in the input envelope.
        sanity = mqtt_pb2.ServiceEnvelope()
        sanity.ParseFromString(envelope_bytes)
        assert sanity.packet.hop_limit == 3
        assert sanity.packet.hop_start == 3

        modified_bytes, old_hop, _meta = zerohop_protobuf(envelope_bytes)

        assert old_hop == 3
        assert modified_bytes is not None

        modified = mqtt_pb2.ServiceEnvelope()
        modified.ParseFromString(modified_bytes)
        assert modified.packet.hop_limit == 0
        assert modified.packet.hop_start == 0, (
            "hop_start must be zeroed too; otherwise receiving firmware "
            "computes hopsTaken = hop_start - hop_limit > 0 and pads the "
            "RouteDiscovery route with 'Meshtastic ffff (ffff)' sentinels"
        )


# ---------------------------------------------------------------------------
# process_message — routing logic with mocked zerohop functions
# ---------------------------------------------------------------------------

class TestProcessMessage:

    def test_non_meshtastic_topic_passthru(self):
        config = _make_config(zerohop_channels=["LongFast"])
        result = process_message("other/topic", b"data", config)
        assert result.action == ACTION_PASSTHRU

    def test_msh_status_topic_silently_skipped(self):
        config = _make_config(zerohop_channels=["LongFast"])
        result = process_message("msh/US/CA", b"data", config)
        assert result.action == ACTION_PASSTHRU

    def test_zerohops_listed_channel_proto(self):
        config = _make_config(zerohop_channels=["LongFast"])
        with patch("floodgate.zerohop.zerohop_protobuf") as mock_zh:
            mock_zh.return_value = (b"modified", 3, {})
            result = process_message("msh/US/CA/2/e/LongFast/!1234", b"proto", config)
        assert result.action == ACTION_MODIFY
        assert result.payload == b"modified"

    def test_zerohops_listed_channel_json(self):
        config = _make_config(zerohop_channels=["LongFast"])
        with patch("floodgate.zerohop.zerohop_json") as mock_zh:
            mock_zh.return_value = (b'{"hop_limit":0}', 3, {})
            result = process_message("msh/US/CA/2/json/LongFast/!1234", b"json", config)
        assert result.action == ACTION_MODIFY
        assert result.payload == b'{"hop_limit":0}'

    def test_zerohops_deep_topic(self):
        config = _make_config(zerohop_channels=["LongFast"])
        with patch("floodgate.zerohop.zerohop_protobuf") as mock_zh:
            mock_zh.return_value = (b"modified", 3, {})
            result = process_message("msh/US/CA/BAY/2/e/LongFast/!16cec9ac", b"proto", config)
        assert result.action == ACTION_MODIFY

    def test_passes_through_unlisted_channel(self):
        config = _make_config(zerohop_channels=["LongFast"])
        result = process_message("msh/US/2/e/MyChannel/!1234", b"proto", config)
        assert result.action == ACTION_PASSTHRU

    def test_passes_through_unlisted_channel_json(self):
        config = _make_config(zerohop_channels=["LongFast"])
        result = process_message("msh/US/2/json/MyChannel/!1234", b"data", config)
        assert result.action == ACTION_PASSTHRU

    def test_already_zero_returns_passthru(self):
        config = _make_config(zerohop_channels=["LongFast"])
        with patch("floodgate.zerohop.zerohop_protobuf") as mock_zh:
            mock_zh.return_value = (None, 0, {})
            result = process_message("msh/US/2/e/LongFast/!1234", b"proto", config)
        assert result.action == ACTION_PASSTHRU

    def test_parse_error_returns_passthru(self):
        config = _make_config(zerohop_channels=["LongFast"])
        with patch("floodgate.zerohop.zerohop_protobuf") as mock_zh:
            mock_zh.return_value = (None, None, {})
            result = process_message("msh/US/2/e/LongFast/!1234", b"bad", config)
        assert result.action == ACTION_PASSTHRU

    def test_zerohop_disabled_passes_listed_channel(self):
        config = _make_config(zerohop_enabled=False, zerohop_channels=["LongFast"])
        result = process_message("msh/US/2/e/LongFast/!1234", b"proto", config)
        assert result.action == ACTION_PASSTHRU

    def test_meta_fields_formatted(self):
        config = _make_config(zerohop_channels=["LongFast"])
        meta = {
            "packet_id":   0xDEADBEEF,
            "sender":      0xAABBCCDD,
            "destination": 0xFFFFFFFF,
            "hop_start":   5,
            "via_mqtt":    True,
            "relay_node":  0xAB,
        }
        with patch("floodgate.zerohop.zerohop_protobuf") as mock_zh:
            mock_zh.return_value = (b"modified", 3, meta)
            result = process_message("msh/US/2/e/LongFast/!1234", b"proto", config)
        assert result.action == ACTION_MODIFY
        assert result.payload == b"modified"


# ---------------------------------------------------------------------------
# Drop flow — runs before zerohop, returns ACTION_DROP
# ---------------------------------------------------------------------------

class TestProcessMessageDrop:

    def _json_payload(self, *, type_="text"):
        return json.dumps({
            "from": 305419896, "to": 4294967295, "id": 999,
            "hop_start": 5, "hops_away": 0, "type": type_,
        }).encode()

    def test_drops_matching_portnum_on_matching_channel(self, caplog):
        config = _make_config(
            zerohop_channels=["LongFast"],
            drop_enabled=True,
            drop_channels=["LongFast"],
            drop_portnums=["TEXT_MESSAGE_APP"],
        )
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!1234",
                self._json_payload(type_="text"),
                config,
            )
        assert result.action == ACTION_DROP
        rec = [r for r in caplog.records if getattr(r, "outcome", None) == "dropped"]
        assert len(rec) == 1
        assert getattr(rec[0], "portnum") == "TEXT_MESSAGE_APP"
        assert getattr(rec[0], "channel") == "LongFast"

    def test_does_not_drop_non_matching_portnum(self):
        config = _make_config(
            zerohop_channels=["LongFast"],
            drop_enabled=True,
            drop_channels=["LongFast"],
            drop_portnums=["RANGE_TEST_APP"],
        )
        with patch("floodgate.zerohop.zerohop_json") as mock_zh:
            mock_zh.return_value = (b'{"hop_limit":0}', 3, {})
            result = process_message(
                "msh/US/2/json/LongFast/!1234",
                self._json_payload(type_="text"),
                config,
            )
        assert result.action == ACTION_MODIFY

    def test_does_not_drop_off_channel(self):
        config = _make_config(
            drop_enabled=True,
            drop_channels=["LongFast"],
            drop_portnums=["TEXT_MESSAGE_APP"],
        )
        result = process_message(
            "msh/US/2/json/MyChannel/!1234",
            self._json_payload(type_="text"),
            config,
        )
        assert result.action == ACTION_PASSTHRU

    def test_drop_disabled_never_drops(self):
        config = _make_config(
            zerohop_channels=["LongFast"],
            drop_enabled=False,
            drop_channels=["LongFast"],
            drop_portnums=["TEXT_MESSAGE_APP"],
        )
        with patch("floodgate.zerohop.zerohop_json") as mock_zh:
            mock_zh.return_value = (b'{"hop_limit":0}', 3, {})
            result = process_message(
                "msh/US/2/json/LongFast/!1234",
                self._json_payload(type_="text"),
                config,
            )
        assert result.action == ACTION_MODIFY

    def test_drop_runs_before_zerohop(self):
        """Drop should short-circuit so zerohop_* is never called."""
        config = _make_config(
            zerohop_channels=["LongFast"],
            drop_enabled=True,
            drop_channels=["LongFast"],
            drop_portnums=["TEXT_MESSAGE_APP"],
        )
        with patch("floodgate.zerohop.zerohop_json") as mock_zh:
            mock_zh.return_value = (b'{"hop_limit":0}', 3, {})
            result = process_message(
                "msh/US/2/json/LongFast/!1234",
                self._json_payload(type_="text"),
                config,
            )
        assert result.action == ACTION_DROP
        mock_zh.assert_not_called()

    def test_drop_channels_none_means_all_channels(self):
        config = _make_config(
            drop_enabled=True,
            drop_channels=None,
            drop_portnums=["TEXT_MESSAGE_APP"],
        )
        # _make_config maps drop_channels=None → _drop_channels_set=None
        result = process_message(
            "msh/US/2/json/AnyChannel/!1234",
            self._json_payload(type_="text"),
            config,
        )
        assert result.action == ACTION_DROP

    def test_unidentifiable_portnum_delivers(self):
        """A JSON payload with no recognizable type can't be classified —
        deliver (don't drop) since drop is destructive."""
        config = _make_config(
            zerohop_channels=["LongFast"],
            drop_enabled=True,
            drop_channels=["LongFast"],
            drop_portnums=["TEXT_MESSAGE_APP"],
        )
        opaque = json.dumps({"from": 1, "to": 2, "id": 3, "hop_limit": 3,
                             "hop_start": 3}).encode()
        with patch("floodgate.zerohop.zerohop_json") as mock_zh:
            mock_zh.return_value = (b'{"hop_limit":0}', 3, {})
            result = process_message(
                "msh/US/2/json/LongFast/!1234", opaque, config,
            )
        assert result.action == ACTION_MODIFY


# ---------------------------------------------------------------------------
# _peek_meta
# ---------------------------------------------------------------------------

class TestPeekMeta:

    def test_json_valid_returns_meta(self):
        payload = json.dumps({
            "id": 12345, "from": 305419896, "to": 4294967295,
            "hop_start": 3, "via_mqtt": True,
        }).encode()
        meta = _peek_meta("json", payload)
        assert meta["packet_id"]   == 12345
        assert meta["sender"]      == 305419896
        assert meta["destination"] == 4294967295
        assert meta["hop_start"]   == 3
        assert meta["via_mqtt"]    is True

    def test_json_invalid_returns_empty(self):
        assert _peek_meta("json", b"not valid json {{") == {}

    def test_json_empty_payload_returns_empty(self):
        assert _peek_meta("json", b"") == {}

    def test_protobuf_invalid_returns_empty(self):
        assert _peek_meta("e", b"not a protobuf") == {}

    def test_protobuf_empty_returns_empty(self):
        assert _peek_meta("e", b"") == {}

    def test_passthru_log_includes_packet_id(self, caplog):
        config = _make_config(zerohop_channels=["LongFast"])
        payload = json.dumps({"id": 99999, "from": 1, "to": 4294967295,
                              "hop_start": 3, "hops_away": 0}).encode()
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/MyPrivate/!aabbccdd", payload, config,
            )
        assert result.action == ACTION_PASSTHRU
        rec = caplog.records[-1]
        assert getattr(rec, "id")      == 99999
        assert getattr(rec, "outcome") == "passthru"


# ---------------------------------------------------------------------------
# Real-world JSON payloads through the full pipeline
# ---------------------------------------------------------------------------

class TestProcessMessageUnmockedJson:
    """Feed real JSON payloads through the full process_message path
    WITHOUT mocking zerohop_json or zerohop_protobuf. This exercises the
    complete decode → modify → log pipeline end-to-end."""

    def _json_payload(self, **overrides):
        """Standard Meshtastic JSON payload with integer node IDs."""
        data = {
            "from":      305419896,
            "to":        4294967295,
            "id":        1700391097,
            "hop_start": 5,
            "hops_away": 0,
            "channel":   1,
            "type":      "text",
        }
        data.update(overrides)
        return json.dumps(data).encode()

    def test_zerohop_integer_node_ids(self, caplog):
        config = _make_config(zerohop_channels=["LongFast"])
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!12345678", self._json_payload(), config,
            )
        assert result.action == ACTION_MODIFY
        assert json.loads(result.payload)["hop_limit"] == 0
        rec = caplog.records[-1]
        assert getattr(rec, "outcome")  == "zerohop"
        assert getattr(rec, "channel")  == "LongFast"
        assert getattr(rec, "encoding") == "json"

    def test_zerohop_string_node_ids(self, caplog):
        """Regression for #24: string node IDs must not crash _fmt_node."""
        config = _make_config(zerohop_channels=["LongFast"])
        data = {
            "from":      "!f6acb04f",
            "to":        "!ffffffff",
            "id":        12345,
            "hop_limit": 3,
            "hop_start": 3,
        }
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!f6acb04f", json.dumps(data).encode(), config,
            )
        assert result.action == ACTION_MODIFY
        rec = caplog.records[-1]
        assert getattr(rec, "outcome")    == "zerohop"
        assert getattr(rec, "from")       == "!f6acb04f"

    def test_noop_when_hops_away_equals_hop_start(self, caplog):
        config  = _make_config(zerohop_channels=["LongFast"])
        payload = self._json_payload(hop_start=5, hops_away=5)
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!12345678", payload, config,
            )
        assert result.action == ACTION_PASSTHRU
        rec = caplog.records[-1]
        assert getattr(rec, "outcome") == "noop"

    def test_explicit_hop_limit_zero_is_noop(self, caplog):
        config  = _make_config(zerohop_channels=["LongFast"])
        payload = self._json_payload(hop_limit=0)
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!12345678", payload, config,
            )
        assert result.action == ACTION_PASSTHRU
        rec = caplog.records[-1]
        assert getattr(rec, "outcome") == "noop"

    def test_missing_optional_fields(self, caplog):
        config = _make_config(zerohop_channels=["LongFast"])
        data   = {"from": 305419896, "to": 4294967295, "id": 999, "hop_limit": 3}
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!12345678", json.dumps(data).encode(), config,
            )
        assert result.action == ACTION_MODIFY
        assert json.loads(result.payload)["hop_limit"] == 0
        rec = caplog.records[-1]
        assert getattr(rec, "outcome")   == "zerohop"
        assert getattr(rec, "hop_start") is None

    def test_malformed_json_returns_passthru(self, caplog):
        config = _make_config(zerohop_channels=["LongFast"])
        with caplog.at_level(logging.WARNING, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!12345678",
                b"not valid json {{{{",
                config,
            )
        assert result.action == ACTION_PASSTHRU
        rec = caplog.records[-1]
        assert getattr(rec, "outcome") == "warn"

    def test_partial_json_missing_required_hop_fields(self, caplog):
        """Payload with from/to/id but no hop_limit/hop_start/hops_away → noop."""
        config = _make_config(zerohop_channels=["LongFast"])
        data   = {"from": 305419896, "to": 4294967295, "id": 42}
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!12345678", json.dumps(data).encode(), config,
            )
        assert result.action == ACTION_PASSTHRU
        rec = caplog.records[-1]
        assert getattr(rec, "outcome") == "noop"

    def test_edge_case_from_zero(self, caplog):
        config  = _make_config(zerohop_channels=["LongFast"])
        payload = self._json_payload(**{"from": 0})
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!00000000", payload, config,
            )
        assert result.action == ACTION_MODIFY
        rec = caplog.records[-1]
        assert getattr(rec, "from") == "!00000000"

    def test_edge_case_to_zero(self, caplog):
        config  = _make_config(zerohop_channels=["LongFast"])
        payload = self._json_payload(to=0)
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!12345678", payload, config,
            )
        assert result.action == ACTION_MODIFY
        rec = caplog.records[-1]
        assert getattr(rec, "to") == "!00000000"

    # -- Passthru path --------------------------------------------------------

    def test_passthru_with_real_payload(self, caplog):
        # Channel not in zerohop_channels → passthru
        config = _make_config(zerohop_channels=["MediumFast"])
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!12345678", self._json_payload(), config,
            )
        assert result.action == ACTION_PASSTHRU
        rec = caplog.records[-1]
        assert getattr(rec, "outcome") == "passthru"
        assert getattr(rec, "id")      == 1700391097

    def test_passthru_string_node_ids(self, caplog):
        config = _make_config(zerohop_channels=["MediumFast"])
        data   = {
            "from":      "!f6acb04f",
            "to":        "!ffffffff",
            "id":        12345,
            "hop_limit": 3,
            "hop_start": 3,
        }
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!f6acb04f", json.dumps(data).encode(), config,
            )
        assert result.action == ACTION_PASSTHRU
        rec = caplog.records[-1]
        assert getattr(rec, "outcome") == "passthru"
        assert getattr(rec, "from")    == "!f6acb04f"

    def test_zerohop_listed_channel_json(self, caplog):
        config = _make_config(zerohop_channels=["LongFast"])
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!12345678", self._json_payload(), config,
            )
        assert result.action == ACTION_MODIFY
        assert json.loads(result.payload)["hop_limit"] == 0
        rec = caplog.records[-1]
        assert getattr(rec, "outcome") == "zerohop"

    @pytest.mark.parametrize(
        "payload_path",
        _list_json_payloads(),
        ids=lambda p: p.stem,
    )
    def test_real_world_payload_smoke(self, payload_path, caplog):
        """Every JSON file in tests/payloads/ must pass through
        process_message without crashing and produce exactly one valid
        outcome record. Drop new samples into the directory to extend
        coverage automatically."""
        config = _make_config(zerohop_channels=["LongFast"])
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            process_message(
                "msh/US/2/json/LongFast/!00000000", payload_path.read_bytes(), config,
            )
        outcome_records = [r for r in caplog.records if hasattr(r, "outcome")]
        assert len(outcome_records) == 1
        assert getattr(outcome_records[0], "outcome") in (
            "zerohop", "noop", "passthru", "warn", "dropped",
        )

    def test_range_test_app_real_world_payload(self, caplog):
        """Real-world RANGE_TEST_APP JSON published by a Meshtastic gateway.

        Two behaviors documented here:

        1. Gateway-published JSON nests hop_limit inside `payload`, not at
           the top level. zerohop_json only inspects the top level, so this
           packet is treated as already zero-hopped (noop) and passes
           through unchanged. (To actually filter range-test traffic, use
           the drop_portnums setting.)

        2. The JSON has both `from` (originating node, decimal int) and
           `sender` (publishing gateway, !hex string) — they may differ.
           floodgate reads `from` for its log fields.
        """
        config = _make_config(zerohop_channels=["LongFast"])
        payload = _load_json_payload("range_test_app.json")
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!9d5f3af4", payload, config,
            )
        assert result.action == ACTION_PASSTHRU
        rec = [r for r in caplog.records if getattr(r, "outcome", None) == "noop"]
        assert len(rec) == 1
        assert getattr(rec[0], "id")        == 1455581347
        # `from` field of the fixture (anonymized to 0xaa00000a)
        assert getattr(rec[0], "from")      == "!aa00000a"
        assert getattr(rec[0], "to")        == "!ffffffff"
        assert getattr(rec[0], "hop_limit") == 0

    def test_range_test_app_dropped_when_configured(self, caplog):
        """End-to-end: drop_portnums actually drops the real-world
        gateway-published RANGE_TEST_APP payload by reading the nested
        portnum field."""
        config = _make_config(
            zerohop_channels=["LongFast"],
            drop_enabled=True,
            drop_portnums=["RANGE_TEST_APP"],
        )
        payload = _load_json_payload("range_test_app.json")
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/json/LongFast/!9d5f3af4", payload, config,
            )
        assert result.action == ACTION_DROP
        rec = [r for r in caplog.records if getattr(r, "outcome", None) == "dropped"]
        assert len(rec) == 1
        assert getattr(rec[0], "portnum") == "RANGE_TEST_APP"


# ---------------------------------------------------------------------------
# Real-world protobuf payloads
# ---------------------------------------------------------------------------

try:
    import meshtastic  # noqa: F401
    _HAS_MESHTASTIC = True
except ImportError:
    _HAS_MESHTASTIC = False


@pytest.mark.skipif(
    not _HAS_MESHTASTIC,
    reason="Meshtastic protobufs not generated. Run scripts/generate_protos.sh",
)
class TestProcessMessageUnmockedProtobuf:
    """Full-path tests: real binary /e/ ServiceEnvelope payloads through
    process_message, no mocks."""

    @pytest.mark.parametrize(
        "payload_path",
        _list_protobuf_payloads(),
        ids=lambda p: p.stem,
    )
    def test_real_world_payload_smoke(self, payload_path, caplog):
        """Every .bin file in tests/payloads/protobuf/ must pass through
        process_message without crashing and produce exactly one valid
        outcome record."""
        config = _make_config(zerohop_channels=["LongFast"])
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            process_message(
                "msh/US/2/e/LongFast/!00000000", payload_path.read_bytes(), config,
            )
        outcome_records = [r for r in caplog.records if hasattr(r, "outcome")]
        assert len(outcome_records) == 1
        assert getattr(outcome_records[0], "outcome") in (
            "zerohop", "noop", "passthru", "warn", "dropped",
        )

    def test_synthetic_envelope_zerohop_round_trip(self, caplog):
        """A real encrypted /e/ ServiceEnvelope built with hop_limit=3 flows
        through process_message, returns ACTION_MODIFY with hop_limit zeroed,
        and the inner Data still decrypts and parses cleanly."""
        from meshtastic import mesh_pb2, mqtt_pb2, portnums_pb2

        from floodgate.decrypt import decrypt as floodgate_decrypt
        from tests.test_portnum import _build_encrypted_envelope

        packet_id = 0xA1B2C3D4
        from_node = 0x12345678
        envelope_bytes = _build_encrypted_envelope(
            mesh_pb2, mqtt_pb2,
            portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            payload_bytes=b"hello",
            packet_id=packet_id,
            from_node=from_node,
            channel_name="LongFast",
            hop_limit=3,
            hop_start=3,
        )

        config = _make_config(zerohop_channels=["LongFast"])
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message(
                "msh/US/2/e/LongFast/!00000000", envelope_bytes, config,
            )

        assert result.action == ACTION_MODIFY
        assert result.payload is not None

        modified = mqtt_pb2.ServiceEnvelope()
        modified.ParseFromString(result.payload)
        assert modified.packet.hop_limit == 0
        assert modified.packet.hop_start == 0
        assert modified.packet.id == packet_id
        assert modified.channel_id == "LongFast"

        plaintext = floodgate_decrypt(
            modified.packet.encrypted, packet_id=packet_id, from_node=from_node,
        )
        inner = mesh_pb2.Data()
        inner.ParseFromString(plaintext)
        assert inner.portnum == portnums_pb2.PortNum.TEXT_MESSAGE_APP
        assert inner.payload == b"hello"

        outcome_records = [r for r in caplog.records if hasattr(r, "outcome")]
        assert len(outcome_records) == 1
        assert getattr(outcome_records[0], "outcome") == "zerohop"


# ---------------------------------------------------------------------------
# AntifloodStats — counter mechanics
# ---------------------------------------------------------------------------

class TestAntifloodStats:

    @pytest.fixture
    def stats(self, monkeypatch):
        fresh = AntifloodStats()
        monkeypatch.setattr("floodgate.zerohop.stats", fresh)
        return fresh

    def test_inc_increments_both_views(self, stats):
        stats.inc("dropped")
        assert stats.dropped == 1
        assert stats.snapshot()["dropped"] == 1

    def test_reset_zeros_rolling_keeps_lifetime(self, stats):
        for _ in range(3):
            stats.inc("dropped")
        snap = stats.reset()
        assert snap["dropped"]                   == 3
        assert stats.dropped              == 0
        assert stats.snapshot()["dropped"] == 3

    def test_total_includes_dropped(self, stats):
        stats.inc("zerohop")
        stats.inc("dropped")
        stats.inc("dropped")
        snap = stats.snapshot()
        assert snap["total"] == 3
