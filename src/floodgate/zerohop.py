"""Core zero-hop logic — processes protobuf (/e/) and JSON (/json/) Meshtastic packets."""

import json as _json
import logging
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_mqtt_pb2 = None
_mesh_pb2 = None


def _load_protos():
    global _mqtt_pb2, _mesh_pb2
    if _mqtt_pb2 is None:
        try:
            from meshtastic import mesh_pb2, mqtt_pb2
            _mqtt_pb2 = mqtt_pb2
            _mesh_pb2 = mesh_pb2
        except ImportError:
            raise ImportError(
                "Meshtastic protobuf modules not found. "
                "Run ./scripts/download_protobufs.sh and ./scripts/generate_protos.sh, "
                "or build via Docker which handles this automatically."
            )


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

_COUNTER_NAMES = ("zerohop", "passthru", "noop", "skipped", "errors")


@dataclass
class AntifloodStats:
    """Thread-safe packet counters.

    Two sets: rolling window (reset each stats interval) and lifetime
    (cumulative, never reset). The stats reporter reads rolling via reset();
    the health endpoint reads lifetime via snapshot().
    """
    # Rolling window — reset by stats reporter each interval
    zerohop:  int = 0
    passthru: int = 0
    noop:     int = 0
    skipped:  int = 0
    errors:   int = 0
    # Lifetime — never reset
    _lifetime: dict = field(default_factory=lambda: {k: 0 for k in _COUNTER_NAMES})
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inc(self, counter: str):
        with self._lock:
            setattr(self, counter, getattr(self, counter) + 1)
            self._lifetime[counter] += 1

    @property
    def total(self) -> int:
        return self.zerohop + self.passthru + self.noop + self.skipped + self.errors

    def snapshot(self) -> dict:
        """Return lifetime cumulative counters (for health endpoint)."""
        with self._lock:
            snap = dict(self._lifetime)
            snap["total"] = sum(self._lifetime.values())
            return snap

    def reset(self) -> dict:
        """Return rolling window snapshot then reset rolling counters to zero."""
        with self._lock:
            snap = {k: getattr(self, k) for k in _COUNTER_NAMES}
            snap["total"] = self.total
            self.zerohop = self.passthru = self.noop = self.skipped = self.errors = 0
            return snap


# Module-level stats instance shared across all calls
stats = AntifloodStats()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_node(node_id: int | None) -> str:
    """Format a Meshtastic node ID as !hex."""
    if node_id is None:
        return "?"
    return f"!{node_id:08x}"


# ---------------------------------------------------------------------------
# Topic parsing
# ---------------------------------------------------------------------------

def parse_meshtastic_topic(topic: str) -> tuple[str, str] | None:
    """Parse a Meshtastic MQTT topic and return (channel, encoding) if it is a
    processable packet topic, otherwise None.

    Supported encodings: 'e' (protobuf), 'json'.
    Non-packet msh/ topics (map reports, stat topics, etc.) return None and
    are silently ignored (no per-message log, counted as 'skipped' in stats).

    Topic format: msh/{...prefix...}/{version}/{encoding}/{channel}/{nodeId}
      The prefix depth varies by deployment, e.g.:
        msh/US/2/e/LongFast/!deadbeef          (2-part prefix)
        msh/US/REG/2/e/LongFast/!deadbeef       (3-part prefix)
        msh/US/REG/AREA/2/e/LongFast/!deadbeef  (4-part prefix)

    We anchor from the right using '!nodeId', so encoding and channel are
    always at parts[-3] and parts[-2] regardless of prefix depth.
    """
    parts = topic.split("/")
    # Minimum: msh / {ver} / {enc} / {channel} / !nodeId  = 5 parts
    if len(parts) < 5 or parts[0] != "msh":
        return None
    # !nodeId is always the last component
    if not parts[-1].startswith("!"):
        return None
    encoding = parts[-3]
    if encoding not in ("e", "json"):
        return None
    return parts[-2], encoding  # (channel, encoding)


# ---------------------------------------------------------------------------
# Payload modification
# ---------------------------------------------------------------------------

def _peek_meta(encoding: str, payload: bytes) -> dict:
    """Parse packet metadata without modifying the payload. Best-effort."""
    if encoding == "json":
        try:
            data = _json.loads(payload)
            meta = {}
            for src_key, meta_key in [("id", "packet_id"), ("from", "sender"),
                                       ("to", "destination"), ("hop_start", "hop_start"),
                                       ("via_mqtt", "via_mqtt")]:
                if src_key in data:
                    meta[meta_key] = data[src_key]
            return meta
        except Exception:
            return {}
    else:
        try:
            _load_protos()
            envelope = _mqtt_pb2.ServiceEnvelope()
            envelope.ParseFromString(payload)
            if envelope.HasField("packet"):
                return _extract_proto_meta(envelope.packet)
        except Exception:
            pass
        return {}


def _extract_proto_meta(pkt) -> dict:
    """Extract packet metadata from a MeshPacket protobuf object.

    Best-effort: the 'from' field conflicts with a Python keyword and its
    generated attribute name varies across protobuf library versions.
    Returns whatever can be extracted; never raises.
    """
    meta = {}
    try:
        for attr, key in [("id", "packet_id"), ("to", "destination"),
                          ("hop_start", "hop_start"), ("via_mqtt", "via_mqtt"),
                          ("relay_node", "relay_node"), ("next_hop", "next_hop")]:
            try:
                val = getattr(pkt, attr, None)
                if val is not None:
                    meta[key] = val
            except Exception:
                pass
        # 'from' is a Python keyword — try protobuf descriptor fields directly
        try:
            fields_by_name = pkt.DESCRIPTOR.fields_by_name
            from_field = fields_by_name.get("from") or fields_by_name.get("from_")
            if from_field is not None:
                meta["sender"] = getattr(pkt, from_field.name, None)
        except Exception:
            pass
        # Fallback: try common attribute names
        if "sender" not in meta:
            for attr in ("from_", "from"):
                try:
                    val = getattr(pkt, attr, None)
                    if val is not None:
                        meta["sender"] = val
                        break
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("Could not extract proto metadata: %s(%s)", type(exc).__name__, exc)
    return meta


def zerohop_protobuf(payload: bytes) -> tuple[bytes | None, int | None, dict]:
    """Zero-hop a protobuf ServiceEnvelope.

    Returns (modified_bytes, original_hop_limit, packet_meta):
      - (None, None, {})  on parse error
      - (None, 0,    meta) if hop_limit was already 0
      - (bytes, N,   meta) on success
    """
    _load_protos()
    try:
        envelope = _mqtt_pb2.ServiceEnvelope()
        envelope.ParseFromString(payload)
        logger.debug("zerohop_protobuf: parsed %d bytes, has_packet=%s",
                     len(payload), envelope.HasField("packet"))

        if not envelope.HasField("packet"):
            logger.debug("ServiceEnvelope has no packet field")
            return None, None, {}

        pkt = envelope.packet
        old_hop = pkt.hop_limit
        logger.debug("zerohop_protobuf: old_hop=%d", old_hop)
        if old_hop == 0:
            meta = _extract_proto_meta(pkt)
            return None, 0, meta

        envelope.packet.hop_limit = 0
        modified = envelope.SerializeToString()
        logger.debug("zerohop_protobuf: serialized %d bytes", len(modified))

    except Exception as exc:
        logger.warning("Failed to parse protobuf payload (%d bytes): %s(%s)",
                       len(payload), type(exc).__name__, exc)
        return None, None, {}

    meta = _extract_proto_meta(pkt)
    return modified, old_hop, meta


def zerohop_json(payload: bytes) -> tuple[bytes | None, int | None, dict]:
    """Zero-hop a JSON-encoded Meshtastic packet.

    Meshtastic JSON format omits 'hop_limit' and uses 'hops_away' instead:
      effective hop_limit = hop_start - hops_away

    Returns (modified_bytes, original_hop_limit, packet_meta):
      - (None, None, {})  on parse error
      - (None, 0,    meta) if effective hop_limit is already 0
      - (bytes, N,   meta) on success (adds hop_limit: 0 to the JSON)
    """
    try:
        data = _json.loads(payload)
        logger.debug("zerohop_json: parsed %d bytes, keys=%s", len(payload), list(data.keys()))
    except Exception as exc:
        logger.warning("Failed to parse JSON payload (%d bytes): %s(%s)",
                       len(payload), type(exc).__name__, exc)
        return None, None, {}

    meta = {}
    for src_key, meta_key in [("id", "packet_id"), ("from", "sender"),
                               ("to", "destination"), ("hop_start", "hop_start"),
                               ("via_mqtt", "via_mqtt")]:
        if src_key in data:
            meta[meta_key] = data[src_key]

    # Meshtastic JSON uses 'hops_away' (hops taken so far), not 'hop_limit'.
    # Effective hop_limit = hop_start - hops_away.
    if "hop_limit" in data:
        old_hop = data["hop_limit"]
    elif "hop_start" in data and "hops_away" in data:
        old_hop = max(0, data["hop_start"] - data["hops_away"])
    else:
        old_hop = 0

    if old_hop == 0:
        return None, 0, meta

    data["hop_limit"] = 0
    return _json.dumps(data).encode(), old_hop, meta


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def process_message(topic: str, payload: bytes, config: dict) -> bytes | None:
    """Process one MQTT message; return modified payload or None (pass-through).

    Outcomes logged at INFO: zerohop, passthru, noop, warn.
    Non-packet topics silently skipped (counted in stats).
    """
    try:
        return _process_message_inner(topic, payload, config)
    except Exception as exc:
        stats.inc("errors")
        logger.error(
            "Unhandled exception processing topic=%s: %s(%s)",
            topic, type(exc).__name__, exc,
            exc_info=True,
        )
        return None


def _process_message_inner(topic: str, payload: bytes, config: dict) -> bytes | None:
    from .config import should_zerohop

    parsed = parse_meshtastic_topic(topic)
    if parsed is None:
        # Map reports, stat topics, non-msh — silently ignore.
        # Counted in 'skipped' for stats but no per-message log line.
        stats.inc("skipped")
        return None

    channel, encoding = parsed

    if not should_zerohop(config, channel):
        stats.inc("passthru")
        meta = _peek_meta(encoding, payload)
        relay = meta.get("relay_node")
        logger.info(
            "passthru",
            extra={
                "event":     "message",
                "outcome":   "passthru",
                "topic":     topic,
                "channel":   channel,
                "encoding":  encoding,
                "id":        meta.get("packet_id"),
                "from":      _fmt_node(meta.get("sender")),
                "to":        _fmt_node(meta.get("destination")),
                "hop_start": meta.get("hop_start"),
                "relay":     f"{relay & 0xFF:02x}" if relay else None,
                "via_mqtt":  meta.get("via_mqtt"),
            },
        )
        return None

    if encoding == "json":
        modified, old_hop, meta = zerohop_json(payload)
    else:
        modified, old_hop, meta = zerohop_protobuf(payload)

    if modified is None and old_hop is None:
        stats.inc("errors")
        logger.warning(
            "warn",
            extra={
                "event":    "message",
                "outcome":  "warn",
                "topic":    topic,
                "channel":  channel,
                "encoding": encoding,
                "bytes":    len(payload),
            },
        )
        return None

    if old_hop == 0:
        stats.inc("noop")
        relay = meta.get("relay_node")
        logger.info(
            "noop",
            extra={
                "event":     "message",
                "outcome":   "noop",
                "topic":     topic,
                "channel":   channel,
                "encoding":  encoding,
                "id":        meta.get("packet_id"),
                "from":      _fmt_node(meta.get("sender")),
                "to":        _fmt_node(meta.get("destination")),
                "hop_limit": 0,
                "hop_start": meta.get("hop_start"),
                "relay":     f"{relay & 0xFF:02x}" if relay else None,
                "via_mqtt":  meta.get("via_mqtt"),
            },
        )
        return None

    stats.inc("zerohop")
    relay = meta.get("relay_node")
    logger.info(
        "zerohop",
        extra={
            "event":     "message",
            "outcome":   "zerohop",
            "topic":     topic,
            "channel":   channel,
            "encoding":  encoding,
            "id":        meta.get("packet_id"),
            "from":      _fmt_node(meta.get("sender")),
            "to":        _fmt_node(meta.get("destination")),
            "hop_limit": old_hop,
            "hop_start": meta.get("hop_start"),
            "relay":     f"{relay & 0xFF:02x}" if relay else None,
            "via_mqtt":  meta.get("via_mqtt"),
        },
    )
    return modified
