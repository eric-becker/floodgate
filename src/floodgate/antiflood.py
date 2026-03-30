"""Core zero-hop logic — processes protobuf (/e/) and JSON (/json/) Meshtastic packets."""

import json as _json
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_mqtt_pb2 = None
_mesh_pb2 = None


def _load_protos():
    global _mqtt_pb2, _mesh_pb2
    if _mqtt_pb2 is None:
        try:
            from meshtastic import mqtt_pb2, mesh_pb2
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

@dataclass
class AntifloodStats:
    """Thread-safe counters for observability."""
    zerohopped: int = 0
    passthru:   int = 0
    noop:       int = 0
    skipped:    int = 0
    errors:     int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inc(self, counter: str):
        with self._lock:
            setattr(self, counter, getattr(self, counter) + 1)

    @property
    def total(self) -> int:
        return self.zerohopped + self.passthru + self.noop + self.skipped + self.errors

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "zerohopped": self.zerohopped,
                "passthru":   self.passthru,
                "noop":       self.noop,
                "skipped":    self.skipped,
                "errors":     self.errors,
                "total":      self.total,
            }

    def reset(self) -> dict:
        """Return a snapshot then reset all counters to zero."""
        with self._lock:
            snap = {
                "zerohopped": self.zerohopped,
                "passthru":   self.passthru,
                "noop":       self.noop,
                "skipped":    self.skipped,
                "errors":     self.errors,
                "total":      self.total,
            }
            self.zerohopped = self.passthru = self.noop = self.skipped = self.errors = 0
            return snap


# Module-level stats instance shared across all calls
stats = AntifloodStats()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_node(node_id: Optional[int]) -> str:
    """Format a full Meshtastic node ID for display."""
    if node_id is None:
        return "?"
    if node_id == 0xFFFFFFFF:
        return "^all"
    return f"!{node_id:08x}"


def _fmt_meta(meta: dict) -> str:
    """Format packet metadata fields for a log line."""
    parts = []
    if meta.get("packet_id"):
        parts.append(f"id={meta['packet_id']}")
    if meta.get("sender") is not None:
        parts.append(f"from={_fmt_node(meta['sender'])}")
    if meta.get("destination") is not None:
        parts.append(f"to={_fmt_node(meta['destination'])}")
    if meta.get("hop_start"):
        parts.append(f"hop_start={meta['hop_start']}")
    if meta.get("via_mqtt"):
        parts.append("via_mqtt=T")
    # relay_node is only the low byte of the relaying node's ID
    relay = meta.get("relay_node")
    if relay:
        parts.append(f"relay={relay & 0xFF:02x}")
    if meta.get("next_hop"):
        parts.append(f"next_hop={_fmt_node(meta['next_hop'])}")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Topic parsing
# ---------------------------------------------------------------------------

def parse_meshtastic_topic(topic: str) -> Optional[Tuple[str, str]]:
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
        _load_protos()
        try:
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


def zerohop_protobuf(payload: bytes) -> Tuple[Optional[bytes], Optional[int], dict]:
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

        if not envelope.HasField("packet"):
            logger.debug("ServiceEnvelope has no packet field")
            return None, None, {}

        pkt = envelope.packet
        old_hop = pkt.hop_limit
        if old_hop == 0:
            meta = _extract_proto_meta(pkt)
            return None, 0, meta

        envelope.packet.hop_limit = 0
        modified = envelope.SerializeToString()

    except Exception as exc:
        logger.warning("Failed to parse protobuf payload (%d bytes): %s(%s)",
                       len(payload), type(exc).__name__, exc)
        return None, None, {}

    meta = _extract_proto_meta(pkt)
    return modified, old_hop, meta


def zerohop_json(payload: bytes) -> Tuple[Optional[bytes], Optional[int], dict]:
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

def process_message(topic: str, payload: bytes, config: dict) -> Optional[bytes]:
    """Process one MQTT message; return modified payload or None (pass-through).

    Non-packet msh/ topics (map reports, stat topics, etc.) are silently
    ignored and counted under 'skipped' in stats.

    Processed packet outcomes are logged at DEBUG level:
      [ZEROHOP]   modified, subscribers receive hop_limit=0
      [PASSTHRU]  channel exempted by policy, subscribers receive original
      [NOOP]      hop_limit was already 0, no change
      [WARN]      payload could not be parsed — original forwarded unchanged
    """
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
        if logger.isEnabledFor(logging.DEBUG):
            meta = _peek_meta(encoding, payload)
            pkt_fields = _fmt_meta(meta)
            logger.debug("[PASSTHRU] topic=%s  channel=%s%s",
                         topic, channel, f"  {pkt_fields}" if pkt_fields else "")
        return None

    if encoding == "json":
        modified, old_hop, meta = zerohop_json(payload)
    else:
        modified, old_hop, meta = zerohop_protobuf(payload)

    pkt_fields = _fmt_meta(meta)

    if modified is None and old_hop is None:
        stats.inc("errors")
        logger.warning(
            "[WARN]     topic=%s  channel=%s  could not parse %d-byte payload",
            topic, channel, len(payload),
        )
        return None

    if old_hop == 0:
        stats.inc("noop")
        logger.debug(
            "[NOOP]     topic=%s  channel=%s  hop_limit already 0%s",
            topic, channel,
            ("  " + pkt_fields) if pkt_fields else "",
        )
        return None

    stats.inc("zerohopped")
    logger.debug(
        "[ZEROHOP]  topic=%s  channel=%s  encoding=%s  hop %d→0%s",
        topic, channel, encoding, old_hop,
        ("  " + pkt_fields) if pkt_fields else "",
    )
    return modified
