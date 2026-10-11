"""Extract Meshtastic portnum names from `/e/` and `/json/` MQTT payloads.

Used by the drop filter to decide whether a packet should be denied.
For `/e/` packets, decryption with the default Meshtastic key is required
(see `decrypt.py`). A custom-keyed packet decrypts to garbage, which almost
always fails to parse and returns None (delivered); rarely, garbage parses as
a valid `Data` message, which is why drop should be scoped to default-key
channels.
"""

import json as _json
import logging

from .decrypt import decrypt

logger = logging.getLogger(__name__)

# Firmware JSON `type` field -> proto enum name. These are exactly the strings
# MeshPacketSerializer::JsonSerialize emits (identical in firmware 2.5.20,
# 2.6.11 and 2.7.11; 2.8 no longer publishes JSON). Every other portnum,
# range test included, is published with an empty `type` and cannot be
# identified, so it is delivered.
_JSON_TYPE_TO_PORTNUM = {
    "text": "TEXT_MESSAGE_APP",
    "telemetry": "TELEMETRY_APP",
    "nodeinfo": "NODEINFO_APP",
    "position": "POSITION_APP",
    "waypoint": "WAYPOINT_APP",
    "neighborinfo": "NEIGHBORINFO_APP",
    "traceroute": "TRACEROUTE_APP",
    "detection": "DETECTION_SENSOR_APP",
    "paxcounter": "PAXCOUNTER_APP",
    "gpios_changed": "REMOTE_HARDWARE_APP",
    "gpios_read_reply": "REMOTE_HARDWARE_APP",
}

_mqtt_pb2 = None
_mesh_pb2 = None
_portnums_pb2 = None


def _load_protos():
    global _mqtt_pb2, _mesh_pb2, _portnums_pb2
    if _mqtt_pb2 is None:
        from meshtastic import mesh_pb2, mqtt_pb2, portnums_pb2

        _mqtt_pb2 = mqtt_pb2
        _mesh_pb2 = mesh_pb2
        _portnums_pb2 = portnums_pb2


def extract_portnum_protobuf(payload: bytes) -> str | None:
    """Return the portnum proto enum name (e.g. "RANGE_TEST_APP") for an
    encrypted ServiceEnvelope payload, or None when it can't be determined.

    Returns None when the envelope is unparseable, the inner packet has no
    encrypted variant, or decryption produces bytes that are not a valid
    Data protobuf — the latter typically means the channel uses a custom
    encryption key that floodgate doesn't have.
    """
    try:
        _load_protos()
        env = _mqtt_pb2.ServiceEnvelope()
        env.ParseFromString(payload)
        if not env.HasField("packet"):
            return None
        pkt = env.packet
        if pkt.WhichOneof("payload_variant") != "encrypted":
            return None
        from_node = _read_from_field(pkt)
        plaintext = decrypt(pkt.encrypted, packet_id=pkt.id, from_node=from_node)
        data = _mesh_pb2.Data()
        data.ParseFromString(plaintext)
        return _portnums_pb2.PortNum.Name(data.portnum)
    except Exception as exc:
        logger.debug("portnum extract (proto) failed: %s(%s)", type(exc).__name__, exc)
        return None


def extract_portnum_json(payload: bytes) -> str | None:
    """Return the portnum proto enum name for a Meshtastic JSON payload, or None.

    Probed in order:
      1. `payload.decoded.portnum`  (gateway packet-wrapper, already an enum name)
      2. top-level `portnum`         (rare: explicit field with the enum name)
      3. `_JSON_TYPE_TO_PORTNUM[type]` (firmware's own JSON output)

    Probes 1 and 2 cover wrapper formats seen in captured traffic from other
    publishers; firmware's JSON only ever sets `type`.
    """
    try:
        data = _json.loads(payload)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    nested = data.get("payload")
    if isinstance(nested, dict):
        decoded = nested.get("decoded")
        if isinstance(decoded, dict):
            pn = decoded.get("portnum")
            if isinstance(pn, str):
                return pn

    pn = data.get("portnum")
    if isinstance(pn, str):
        return pn

    type_str = data.get("type")
    if isinstance(type_str, str):
        return _JSON_TYPE_TO_PORTNUM.get(type_str.lower())

    return None


def _read_from_field(pkt) -> int:
    """Read MeshPacket.from (Python keyword — name varies by protobuf version)."""
    fields = pkt.DESCRIPTOR.fields_by_name
    field = fields.get("from") or fields.get("from_")
    if field is None:
        return 0
    return getattr(pkt, field.name, 0) or 0
