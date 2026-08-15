"""Round-trip fidelity: floodgate must hand back the same packet, minus the hops.

`zerohop_protobuf` does not patch two fields in place — it parses the whole
ServiceEnvelope and re-serialises it. Every field in the message therefore makes
a round trip through the Python protobuf runtime on every zero-hopped packet, so
the blast radius of a regression is the entire envelope, not just `hop_limit`
and `hop_start`.

That matters more than it looks. Meshtastic derives the AES-CTR nonce from the
packet id and the sender id (see `decrypt.build_nonce`), and both live *outside*
the encrypted blob. A change that perturbed either would leave the ciphertext
byte-identical and the envelope structurally valid, while making the payload
permanently undecryptable to every receiver on the mesh. floodgate would log a
successful modify and EMQX would deliver it; the packet would simply never
render anywhere. Comparing the fields we happened to think of would not catch
it — only decrypting the result does.

These tests pin the three properties that would:

  1. the ciphertext survives byte-for-byte,
  2. the packet still decrypts to the original Data, and
  3. `hop_limit` / `hop_start` are the *only* fields that change.

The existing fixtures under `tests/payloads/protobuf/` are anonymised, which
breaks the nonce relationship and makes their ciphertext undecryptable by
design — good for "must not raise" fuzzing, useless for a decrypt assertion. So
these tests build fresh envelopes encrypted under the real default key, the same
approach `test_portnum` takes.
"""

import pytest

from floodgate.decrypt import decrypt
from floodgate.zerohop import zerohop_protobuf
from tests.test_portnum import _build_encrypted_envelope, meshtastic_pb2  # noqa: F401

# Sent with hops remaining — zerohop_protobuf short-circuits on hop_limit == 0
# and returns (None, 0, meta) without re-serialising, which would vacuously
# pass every assertion below.
HOP_LIMIT = 3
HOP_START = 3

PACKET_ID = 0xAABBCCDD
FROM_NODE = 0x12345678


def _portnum_cases(portnums_pb2):
    """Realistic payloads across the portnums that actually fly on the public
    channels. Payload bytes are opaque to floodgate — what matters is that each
    case exercises a different Data shape through the same re-serialisation."""
    P = portnums_pb2.PortNum
    return {
        "text_message":  (P.TEXT_MESSAGE_APP, b"hello mesh"),
        "position":      (P.POSITION_APP, bytes.fromhex("0d8a5f2e15")),
        "nodeinfo":      (P.NODEINFO_APP, b"\x0a\x09!12345678"),
        "telemetry":     (P.TELEMETRY_APP, b"\x0d\x00\x00\x80\x3f"),
        "neighborinfo":  (P.NEIGHBORINFO_APP, b"\x08\x81\x82\x83\x84\x08"),
        "range_test":    (P.RANGE_TEST_APP, b"seq 7"),
    }


def _zerohop_or_fail(envelope_bytes):
    """Run zerohop and assert it actually modified something."""
    modified, old_hop, _meta = zerohop_protobuf(envelope_bytes)
    assert modified is not None, "zerohop_protobuf declined to modify the packet"
    assert old_hop == HOP_LIMIT
    return modified


class TestCiphertextSurvivesZerohop:
    """The encrypted blob is payload floodgate has no business touching."""

    @pytest.mark.parametrize("case", ["text_message", "position", "nodeinfo",
                                      "telemetry", "neighborinfo", "range_test"])
    def test_encrypted_bytes_are_byte_identical(self, meshtastic_pb2, case):  # noqa: F811
        mesh_pb2, mqtt_pb2, portnums_pb2 = meshtastic_pb2
        portnum, payload = _portnum_cases(portnums_pb2)[case]

        envelope_bytes = _build_encrypted_envelope(
            mesh_pb2, mqtt_pb2, portnum=portnum, payload_bytes=payload,
            packet_id=PACKET_ID, from_node=FROM_NODE,
            hop_limit=HOP_LIMIT, hop_start=HOP_START,
        )
        before = mqtt_pb2.ServiceEnvelope()
        before.ParseFromString(envelope_bytes)

        after = mqtt_pb2.ServiceEnvelope()
        after.ParseFromString(_zerohop_or_fail(envelope_bytes))

        assert after.packet.encrypted == before.packet.encrypted

    def test_packet_still_decrypts_to_the_original_data(self, meshtastic_pb2):  # noqa: F811
        """The assertion field-equality checks cannot make.

        Decryption depends on packet id and sender id, which live outside the
        ciphertext. If either were perturbed the bytes would still compare equal
        to themselves — but would decrypt to garbage.
        """
        mesh_pb2, mqtt_pb2, portnums_pb2 = meshtastic_pb2
        original = mesh_pb2.Data()
        original.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        original.payload = b"the mesh must go on"

        envelope_bytes = _build_encrypted_envelope(
            mesh_pb2, mqtt_pb2,
            portnum=original.portnum, payload_bytes=original.payload,
            packet_id=PACKET_ID, from_node=FROM_NODE,
            hop_limit=HOP_LIMIT, hop_start=HOP_START,
        )

        after = mqtt_pb2.ServiceEnvelope()
        after.ParseFromString(_zerohop_or_fail(envelope_bytes))

        # Decrypt using the ids as they survive in the *modified* packet — this
        # is exactly what a receiving node does, so a perturbed id fails here.
        recovered = mesh_pb2.Data()
        recovered.ParseFromString(decrypt(
            after.packet.encrypted,
            packet_id=after.packet.id,
            from_node=getattr(after.packet, "from"),
        ))

        assert recovered.portnum == original.portnum
        assert recovered.payload == original.payload

    def test_nonce_inputs_are_unchanged(self, meshtastic_pb2):  # noqa: F811
        """Pin the two fields the nonce is derived from, explicitly."""
        mesh_pb2, mqtt_pb2, portnums_pb2 = meshtastic_pb2
        envelope_bytes = _build_encrypted_envelope(
            mesh_pb2, mqtt_pb2,
            portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP, payload_bytes=b"x",
            packet_id=PACKET_ID, from_node=FROM_NODE,
            hop_limit=HOP_LIMIT, hop_start=HOP_START,
        )
        after = mqtt_pb2.ServiceEnvelope()
        after.ParseFromString(_zerohop_or_fail(envelope_bytes))

        assert after.packet.id == PACKET_ID
        assert getattr(after.packet, "from") == FROM_NODE


class TestBlastRadius:
    """`hop_limit` and `hop_start` are the only fields allowed to move."""

    @staticmethod
    def _field_map(msg):
        """Set fields as {name: value}, so a dropped field is a missing key
        rather than a silently-equal default."""
        return {fd.name: val for fd, val in msg.ListFields()}

    def test_only_hop_fields_differ(self, meshtastic_pb2):  # noqa: F811
        mesh_pb2, mqtt_pb2, portnums_pb2 = meshtastic_pb2
        envelope_bytes = _build_encrypted_envelope(
            mesh_pb2, mqtt_pb2,
            portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP, payload_bytes=b"blast",
            packet_id=PACKET_ID, from_node=FROM_NODE,
            hop_limit=HOP_LIMIT, hop_start=HOP_START,
        )
        before = mqtt_pb2.ServiceEnvelope()
        before.ParseFromString(envelope_bytes)
        after = mqtt_pb2.ServiceEnvelope()
        after.ParseFromString(_zerohop_or_fail(envelope_bytes))

        before_fields = self._field_map(before.packet)
        after_fields = self._field_map(after.packet)

        # hop_limit/hop_start become 0, which proto3 treats as unset — so they
        # leave the map entirely rather than comparing equal to 0.
        assert set(before_fields) - set(after_fields) <= {"hop_limit", "hop_start"}
        assert set(after_fields) - set(before_fields) == set()

        for name in set(after_fields) & set(before_fields):
            assert after_fields[name] == before_fields[name], f"field {name!r} changed"

    def test_envelope_metadata_survives(self, meshtastic_pb2):  # noqa: F811
        """channel_id and gateway_id route the packet — losing them is fatal."""
        mesh_pb2, mqtt_pb2, portnums_pb2 = meshtastic_pb2
        envelope_bytes = _build_encrypted_envelope(
            mesh_pb2, mqtt_pb2,
            portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP, payload_bytes=b"meta",
            packet_id=PACKET_ID, from_node=FROM_NODE, channel_name="LongFast",
            hop_limit=HOP_LIMIT, hop_start=HOP_START,
        )
        after = mqtt_pb2.ServiceEnvelope()
        after.ParseFromString(_zerohop_or_fail(envelope_bytes))
        assert after.channel_id == "LongFast"

    def test_hop_fields_are_actually_zeroed(self, meshtastic_pb2):  # noqa: F811
        mesh_pb2, mqtt_pb2, portnums_pb2 = meshtastic_pb2
        envelope_bytes = _build_encrypted_envelope(
            mesh_pb2, mqtt_pb2,
            portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP, payload_bytes=b"z",
            packet_id=PACKET_ID, from_node=FROM_NODE,
            hop_limit=HOP_LIMIT, hop_start=HOP_START,
        )
        after = mqtt_pb2.ServiceEnvelope()
        after.ParseFromString(_zerohop_or_fail(envelope_bytes))
        assert after.packet.hop_limit == 0
        assert after.packet.hop_start == 0


class TestForwardCompatibility:
    """Fields from firmware newer than our generated stubs must survive.

    floodgate pins the Meshtastic protobufs it was built against. A gateway
    running newer firmware can emit fields our stubs don't know, which arrive as
    protobuf *unknown fields*. Parse-then-serialise preserves those by default —
    but a refactor that rebuilt the envelope field-by-field instead of mutating
    it in place would silently strip them, and no existing test would notice.
    """

    @staticmethod
    def _unknown_field(field_number, payload):
        """Hand-encode a length-delimited field the schema has no name for."""
        key = (field_number << 3) | 2  # wire type 2 = length-delimited
        out = bytearray()
        for value in (key, len(payload)):
            while True:
                byte = value & 0x7F
                value >>= 7
                out.append(byte | (0x80 if value else 0))
                if not value:
                    break
        return bytes(out) + payload

    def test_unknown_fields_are_preserved(self, meshtastic_pb2):  # noqa: F811
        mesh_pb2, mqtt_pb2, portnums_pb2 = meshtastic_pb2
        envelope_bytes = _build_encrypted_envelope(
            mesh_pb2, mqtt_pb2,
            portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP, payload_bytes=b"future",
            packet_id=PACKET_ID, from_node=FROM_NODE,
            hop_limit=HOP_LIMIT, hop_start=HOP_START,
        )
        # Field 900 is not in the ServiceEnvelope schema and won't be soon.
        marker = self._unknown_field(900, b"from-newer-firmware")
        modified = _zerohop_or_fail(envelope_bytes + marker)

        assert marker in modified, "unknown field was stripped during re-serialisation"


class TestTracerouteFidelity:
    """Regression guard for #46 at the payload level, not the field level.

    The ghost-hop bug was a *rendering* fault: clients derive the hop count from
    hop_start, so zeroing hop_limit alone inflated the count and produced
    phantom 'Meshtastic ffff (ffff)' entries. Asserting hop_start == 0 catches
    the fix; asserting the route array survives catches the overcorrection —
    zeroing something that carries the actual traceroute result.
    """

    def test_route_array_survives_and_hops_are_zeroed(self, meshtastic_pb2):  # noqa: F811
        mesh_pb2, mqtt_pb2, portnums_pb2 = meshtastic_pb2

        route = mesh_pb2.RouteDiscovery()
        route.route.extend([0x11111111, 0x22222222, 0x33333333])
        route_bytes = route.SerializeToString()

        envelope_bytes = _build_encrypted_envelope(
            mesh_pb2, mqtt_pb2,
            portnum=portnums_pb2.PortNum.TRACEROUTE_APP, payload_bytes=route_bytes,
            packet_id=PACKET_ID, from_node=FROM_NODE,
            hop_limit=HOP_LIMIT, hop_start=HOP_START,
        )
        after = mqtt_pb2.ServiceEnvelope()
        after.ParseFromString(_zerohop_or_fail(envelope_bytes))

        recovered_data = mesh_pb2.Data()
        recovered_data.ParseFromString(decrypt(
            after.packet.encrypted,
            packet_id=after.packet.id,
            from_node=getattr(after.packet, "from"),
        ))
        recovered_route = mesh_pb2.RouteDiscovery()
        recovered_route.ParseFromString(recovered_data.payload)

        assert list(recovered_route.route) == [0x11111111, 0x22222222, 0x33333333]
        assert after.packet.hop_limit == 0
        assert after.packet.hop_start == 0
