"""Tests for core zero-hop packet processing logic."""

import json
import logging
from unittest.mock import patch

from floodgate.zerohop import (
    _peek_meta,
    parse_meshtastic_topic,
    process_message,
    zerohop_json,
)


class TestParseMetastasticTopic:

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
        assert parse_meshtastic_topic("msh/US/FL/2/e/LongFast/!16cec9ac") == ("LongFast", "e")

    def test_deep_topic_four_prefix_parts(self):
        # msh/{country}/{region}/{area}/2/e/LongFast/!nodeId — 4-part prefix
        assert parse_meshtastic_topic("msh/US/FL/LWS/2/e/LongFast/!087a5a9c") == ("LongFast", "e")

    def test_json_encoding_deep(self):
        assert parse_meshtastic_topic("msh/US/FL/2/json/LongFast/!deadbeef") == ("LongFast", "json")

    def test_stat_topic_not_a_packet(self):
        # Status topics like msh/US/FL don't end with !nodeId
        assert parse_meshtastic_topic("msh/US/FL") is None

    def test_no_nodeid_suffix(self):
        # msh/ prefix but no !nodeId at the end
        assert parse_meshtastic_topic("msh/US/FL/stat/somenode") is None

    def test_map_report_topic(self):
        # Map report published directly to region root — not a packet
        assert parse_meshtastic_topic("msh/US/FL/map") is None

    def test_unknown_encoding_returns_none(self):
        # Unknown encoding segment
        assert parse_meshtastic_topic("msh/US/2/xml/LongFast/!deadbeef") is None


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
        modified, old_hop, meta = zerohop_json(self._payload(hop_limit=3))
        assert old_hop == 3
        assert modified is not None
        assert json.loads(modified)["hop_limit"] == 0

    def test_uses_hops_away_when_no_hop_limit(self):
        # Meshtastic JSON omits hop_limit, uses hop_start and hops_away
        modified, old_hop, meta = zerohop_json(self._payload_meshtastic(hop_start=5, hops_away=0))
        assert old_hop == 5   # effective: hop_start(5) - hops_away(0)
        assert modified is not None
        assert json.loads(modified)["hop_limit"] == 0

    def test_hops_away_equals_hop_start_is_noop(self):
        # hop_start=5, hops_away=5 → effective hop_limit=0 → already zerohoped
        modified, old_hop, meta = zerohop_json(self._payload_meshtastic(hop_start=5, hops_away=5))
        assert modified is None
        assert old_hop == 0

    def test_noop_when_explicit_hop_limit_zero(self):
        modified, old_hop, meta = zerohop_json(self._payload(hop_limit=0))
        assert modified is None
        assert old_hop == 0

    def test_extracts_metadata(self):
        _, _, meta = zerohop_json(self._payload(hop_limit=5, hop_start=5, via_mqtt=True))
        assert meta["sender"] == 305419896
        assert meta["destination"] == 4294967295
        assert meta["packet_id"] == 12345
        assert meta["hop_start"] == 5
        assert meta["via_mqtt"] is True

    def test_packet_id_in_meta_is_decimal(self):
        _, _, meta = zerohop_json(self._payload_meshtastic())
        assert meta["packet_id"] == 1700391097  # decimal, not hex

    def test_parse_error_returns_none_none(self):
        modified, old_hop, meta = zerohop_json(b"not json at all")
        assert modified is None
        assert old_hop is None

    def test_preserves_other_fields(self):
        modified, _, _ = zerohop_json(self._payload(hop_limit=3))
        data = json.loads(modified)
        assert data["type"] == "text"
        assert data["channel"] == 0


class TestProcessMessage:
    """Test process_message routing logic using mocked zerohop functions."""

    def _config(self, policy="whitelist", whitelist=None, blacklist=None):
        return {
            "channel_policy": policy,
            "_whitelist_set": set(whitelist or []),
            "_blacklist_set": set(blacklist or []),
            "channel_whitelist": whitelist or [],
            "channel_blacklist": blacklist or [],
        }

    def test_non_meshtastic_topic_ignored(self):
        config = self._config()
        assert process_message("other/topic", b"data", config) is None

    def test_msh_status_topic_silently_skipped(self):
        # msh/US/FL (no !nodeId) — map report topic, must be silently ignored
        config = self._config()
        assert process_message("msh/US/FL", b"data", config) is None

    def test_whitelist_empty_zerohops_proto(self):
        config = self._config(policy="whitelist")
        with patch("floodgate.zerohop.zerohop_protobuf") as mock_zh:
            mock_zh.return_value = (b"modified", 3, {})
            result = process_message("msh/US/FL/2/e/LongFast/!1234", b"proto", config)
        assert result == b"modified"

    def test_whitelist_empty_zerohops_json(self):
        config = self._config(policy="whitelist")
        with patch("floodgate.zerohop.zerohop_json") as mock_zh:
            mock_zh.return_value = (b'{"hop_limit":0}', 3, {})
            result = process_message("msh/US/FL/2/json/LongFast/!1234", b"json", config)
        assert result == b'{"hop_limit":0}'

    def test_whitelist_empty_zerohops_deep_topic(self):
        config = self._config(policy="whitelist")
        with patch("floodgate.zerohop.zerohop_protobuf") as mock_zh:
            mock_zh.return_value = (b"modified", 3, {})
            result = process_message("msh/US/FL/LWS/2/e/LongFast/!16cec9ac", b"proto", config)
        assert result == b"modified"

    def test_whitelist_bypasses_listed_channel(self):
        config = self._config(policy="whitelist", whitelist=["MyChannel"])
        result = process_message("msh/US/2/e/MyChannel/!1234", b"proto", config)
        assert result is None

    def test_whitelist_bypasses_listed_channel_json(self):
        config = self._config(policy="whitelist", whitelist=["MyChannel"])
        result = process_message("msh/US/2/json/MyChannel/!1234", b"data", config)
        assert result is None

    def test_blacklist_zerohops_listed(self):
        config = self._config(policy="blacklist", blacklist=["LongFast"])
        with patch("floodgate.zerohop.zerohop_protobuf") as mock_zh:
            mock_zh.return_value = (b"modified", 3, {})
            result = process_message("msh/US/2/e/LongFast/!1234", b"proto", config)
        assert result == b"modified"

    def test_blacklist_passes_unlisted(self):
        config = self._config(policy="blacklist", blacklist=["LongFast"])
        result = process_message("msh/US/2/e/MyChannel/!1234", b"proto", config)
        assert result is None

    def test_already_zero_returns_none(self):
        config = self._config(policy="whitelist")
        with patch("floodgate.zerohop.zerohop_protobuf") as mock_zh:
            mock_zh.return_value = (None, 0, {})
            result = process_message("msh/US/2/e/LongFast/!1234", b"proto", config)
        assert result is None

    def test_parse_error_returns_none(self):
        config = self._config(policy="whitelist")
        with patch("floodgate.zerohop.zerohop_protobuf") as mock_zh:
            mock_zh.return_value = (None, None, {})
            result = process_message("msh/US/2/e/LongFast/!1234", b"bad", config)
        assert result is None

    def test_meta_fields_formatted(self):
        config = self._config(policy="whitelist")
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
        assert result == b"modified"


class TestPeekMeta:
    """Tests for _peek_meta — metadata extraction without payload modification."""

    def test_json_valid_returns_meta(self):
        payload = json.dumps({
            "id": 12345, "from": 305419896, "to": 4294967295,
            "hop_start": 3, "via_mqtt": True,
        }).encode()
        meta = _peek_meta("json", payload)
        assert meta["packet_id"] == 12345
        assert meta["sender"] == 305419896
        assert meta["destination"] == 4294967295
        assert meta["hop_start"] == 3
        assert meta["via_mqtt"] is True

    def test_json_invalid_returns_empty(self):
        meta = _peek_meta("json", b"not valid json {{")
        assert meta == {}

    def test_json_empty_payload_returns_empty(self):
        meta = _peek_meta("json", b"")
        assert meta == {}

    def test_protobuf_invalid_returns_empty(self):
        meta = _peek_meta("e", b"not a protobuf")
        assert meta == {}

    def test_protobuf_empty_returns_empty(self):
        meta = _peek_meta("e", b"")
        assert meta == {}

    def test_passthru_log_includes_packet_id(self, caplog):
        config = {
            "channel_policy": "whitelist",
            "_whitelist_set": {"LongFast"},
            "_blacklist_set": set(),
            "channel_whitelist": ["LongFast"],
            "channel_blacklist": [],
        }
        payload = json.dumps({"id": 99999, "from": 1, "to": 4294967295,
                              "hop_start": 3, "hops_away": 0}).encode()
        with caplog.at_level(logging.INFO, logger="floodgate.zerohop"):
            result = process_message("msh/US/2/json/LongFast/!aabbccdd", payload, config)
        assert result is None
        assert "99999" in caplog.text
