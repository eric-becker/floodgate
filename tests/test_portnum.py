"""Tests for portnum extraction from /e/ and /json/ payloads."""

import json
from pathlib import Path

import pytest

from floodgate.decrypt import decrypt
from floodgate.portnum import extract_portnum_json, extract_portnum_protobuf

PAYLOADS_DIR = Path(__file__).parent / "payloads"
PROTOBUF_PAYLOADS_DIR = PAYLOADS_DIR / "protobuf"


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

class TestExtractPortnumJson:

    # File → expected portnum enum name. Pinned to real fixtures so any
    # parser regression that misclassifies a real-world payload fails here.
    EXPECTED = {
        "text_message.json":         "TEXT_MESSAGE_APP",
        "position.json":             "POSITION_APP",
        "nodeinfo.json":             "NODEINFO_APP",
        "device_metrics.json":       "TELEMETRY_APP",
        "environment_metrics.json":  "TELEMETRY_APP",
        "neighborinfo.json":         "NEIGHBORINFO_APP",
        "traceroute.json":           "TRACEROUTE_APP",
        "range_test_app.json":       "RANGE_TEST_APP",
    }

    @pytest.mark.parametrize("filename,expected", sorted(EXPECTED.items()))
    def test_real_world_fixture(self, filename, expected):
        path = PAYLOADS_DIR / filename
        result = extract_portnum_json(path.read_bytes())
        assert result == expected, f"{filename}: got {result!r}, want {expected!r}"

    def test_invalid_json_returns_none(self):
        assert extract_portnum_json(b"not json {{") is None

    def test_empty_bytes_returns_none(self):
        assert extract_portnum_json(b"") is None

    def test_top_level_array_returns_none(self):
        assert extract_portnum_json(b"[1, 2, 3]") is None

    def test_unknown_type_returns_none(self):
        payload = json.dumps({"type": "made_up_thing"}).encode()
        assert extract_portnum_json(payload) is None

    def test_explicit_top_level_portnum_field(self):
        # Some senders put portnum at the top level directly.
        payload = json.dumps({"portnum": "ADMIN_APP"}).encode()
        assert extract_portnum_json(payload) == "ADMIN_APP"

    def test_nested_decoded_portnum_wins_over_type(self):
        # Wrapper packet form: `type` is "packet", real portnum nested deep.
        payload = json.dumps({
            "type": "packet",
            "payload": {"decoded": {"portnum": "RANGE_TEST_APP"}},
        }).encode()
        assert extract_portnum_json(payload) == "RANGE_TEST_APP"

    def test_payload_field_not_dict_falls_through(self):
        # E.g. payload is a string, not a wrapper dict — fall through to type.
        payload = json.dumps({"type": "text", "payload": "raw string"}).encode()
        assert extract_portnum_json(payload) == "TEXT_MESSAGE_APP"

    def test_type_case_insensitive(self):
        payload = json.dumps({"type": "TEXT"}).encode()
        assert extract_portnum_json(payload) == "TEXT_MESSAGE_APP"


# ---------------------------------------------------------------------------
# Protobuf extraction — synthetic envelopes only.
#
# The captured `tests/payloads/protobuf/*.bin` fixtures were anonymized
# (sender IDs rewritten), which breaks the AES-CTR nonce relationship and
# makes their original ciphertext non-decryptable. We build fresh synthetic
# envelopes encrypted under the default key for these tests.
# ---------------------------------------------------------------------------

@pytest.fixture
def meshtastic_pb2():
    pytest.importorskip("meshtastic")
    from meshtastic import mesh_pb2, mqtt_pb2, portnums_pb2
    return mesh_pb2, mqtt_pb2, portnums_pb2


def _build_encrypted_envelope(mesh_pb2, mqtt_pb2, portnum, payload_bytes,
                              packet_id=0xAABBCCDD, from_node=0x12345678,
                              channel_name="LongFast", hop_limit=0,
                              hop_start=None):
    """Construct a ServiceEnvelope whose inner MeshPacket has the named
    portnum encrypted under the default Meshtastic key."""
    data = mesh_pb2.Data()
    data.portnum = portnum
    data.payload = payload_bytes
    ciphertext = decrypt(data.SerializeToString(),
                         packet_id=packet_id, from_node=from_node)

    pkt = mesh_pb2.MeshPacket()
    pkt.id = packet_id
    setattr(pkt, pkt.DESCRIPTOR.fields_by_name["from"].name, from_node)
    pkt.encrypted = ciphertext
    pkt.hop_limit = hop_limit
    if hop_start is not None:
        pkt.hop_start = hop_start

    env = mqtt_pb2.ServiceEnvelope()
    env.packet.CopyFrom(pkt)
    env.channel_id = channel_name
    return env.SerializeToString()


class TestExtractPortnumProtobuf:

    def test_synthetic_range_test_packet(self, meshtastic_pb2):
        mesh_pb2, mqtt_pb2, portnums_pb2 = meshtastic_pb2
        envelope = _build_encrypted_envelope(
            mesh_pb2, mqtt_pb2,
            portnum=portnums_pb2.PortNum.RANGE_TEST_APP,
            payload_bytes=b"seq 7",
        )
        assert extract_portnum_protobuf(envelope) == "RANGE_TEST_APP"

    def test_synthetic_text_message(self, meshtastic_pb2):
        mesh_pb2, mqtt_pb2, portnums_pb2 = meshtastic_pb2
        envelope = _build_encrypted_envelope(
            mesh_pb2, mqtt_pb2,
            portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            payload_bytes=b"hi",
        )
        assert extract_portnum_protobuf(envelope) == "TEXT_MESSAGE_APP"

    def test_unparseable_bytes_returns_none(self):
        assert extract_portnum_protobuf(b"\x00\x01\x02 nonsense") is None

    def test_empty_bytes_returns_none(self):
        assert extract_portnum_protobuf(b"") is None

    def test_envelope_without_packet_returns_none(self, meshtastic_pb2):
        _, mqtt_pb2, _ = meshtastic_pb2
        env = mqtt_pb2.ServiceEnvelope()
        env.channel_id = "LongFast"
        # No packet field set
        assert extract_portnum_protobuf(env.SerializeToString()) is None

    def test_packet_with_decoded_variant_returns_none(self, meshtastic_pb2):
        mesh_pb2, mqtt_pb2, portnums_pb2 = meshtastic_pb2
        # Already-decoded packet (no encryption) — drop filter doesn't act on these.
        pkt = mesh_pb2.MeshPacket()
        pkt.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        env = mqtt_pb2.ServiceEnvelope()
        env.packet.CopyFrom(pkt)
        assert extract_portnum_protobuf(env.SerializeToString()) is None

    def test_anonymized_real_fixtures_dont_crash(self, meshtastic_pb2):
        """The captured `.bin` fixtures were anonymized (from-field rewritten),
        so AES-CTR decryption with the default key produces garbage. The
        function must return None gracefully — no exceptions, no crashes."""
        if not PROTOBUF_PAYLOADS_DIR.exists():
            pytest.skip("no protobuf fixtures present")
        for path in sorted(PROTOBUF_PAYLOADS_DIR.glob("*.bin")):
            result = extract_portnum_protobuf(path.read_bytes())
            # We don't assert what it returns — only that it doesn't raise.
            # In practice every anonymized fixture decrypts to garbage and
            # returns None, but a chance hash-collision could land on a valid
            # Data parse. Either way: never an exception.
            assert result is None or isinstance(result, str)
