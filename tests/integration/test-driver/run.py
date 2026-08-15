"""Integration test driver.

Runs each integration case sequentially. For every case:
  1. Crafts a Meshtastic ServiceEnvelope (or borrows one from meshtasticd).
  2. Publishes it via MQTT to a topic whose channel name fits the case.
  3. Waits briefly for floodgate to process and EMQX to deliver.
  4. Asserts on (a) what the bound subscriber received and (b) floodgate's
     /health stats, then prints one 'PASS: <case>' or 'FAIL: <case>: <reason>' line.

Exits 0 iff every case passed.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass

import paho.mqtt.client as mqtt
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from meshtastic import (  # noqa: F401  (portnums re-exported for cases)
    mesh_pb2,
    mqtt_pb2,
    portnums_pb2,
)

EMQX_HOST           = os.environ.get("EMQX_HOST", "emqx")
EMQX_PORT           = int(os.environ.get("EMQX_PORT", "1883"))
FLOODGATE_HEALTH    = os.environ.get("FLOODGATE_HEALTH_URL", "http://floodgate:8080/health")

# 16-byte AES-128 key derived from the default Meshtastic PSK ("AQ==").
DEFAULT_KEY = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")
# Arbitrary 16-byte key that floodgate does NOT have — used to simulate a
# custom-keyed channel where floodgate cannot decrypt the inner Data.
CUSTOM_KEY  = bytes.fromhex("00112233445566778899aabbccddeeff")

# Settle window between publish and assertion. EMQX + floodgate gRPC + EMQX
# delivery is normally <100ms on a local bridge; 1s gives plenty of margin
# without making the suite slow.
SETTLE_SECONDS = 1.0

# A failing hook is slower than a working one: a dead server refuses at once,
# but a hung one burns the full ExHook request_timeout (5s in this harness)
# before EMQX gives up and moves on. Wait past that so the assertion isn't
# racing the broker.
HOOK_FAILURE_SETTLE_SECONDS = float(os.environ.get("HOOK_FAILURE_SETTLE_SECONDS", "9.0"))

# EMQX REST, used only to read ExHook metrics. These are the harness's own
# throwaway dashboard defaults, matching tests/integration/exhook-init.
EMQX_API   = os.environ.get("EMQX_API_URL", "http://emqx:18083")
EMQX_USER  = os.environ.get("EMQX_USER", "admin")
EMQX_PASS  = os.environ.get("EMQX_PASS", "public")
HOOK_NAME  = os.environ.get("HOOK_NAME", "floodgate")


def exhook_failed_count() -> int | None:
    """ExHook `metrics.failed` for our hook, or None if EMQX REST is unreadable.

    This is the *only* place a floodgate outage is visible. floodgate itself is
    down, so it logs nothing, and the packet is delivered normally — from the
    subscriber's side nothing looks wrong at all. Monitoring has to watch this
    counter (or EMQX's `exhook_call_exception` error log) to notice that the
    anti-flood protection has silently stopped.
    """
    try:
        token = requests.post(
            f"{EMQX_API}/api/v5/login",
            json={"username": EMQX_USER, "password": EMQX_PASS},
            timeout=5,
        ).json()["token"]
        body = requests.get(
            f"{EMQX_API}/api/v5/exhooks/{HOOK_NAME}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        ).json()
        return int(body["metrics"]["failed"])
    except Exception as exc:  # noqa: BLE001 — diagnostic only, never fatal
        print(f"  (exhook metrics unavailable: {type(exc).__name__}: {exc})", flush=True)
        return None


# ---------------------------------------------------------------------------
# Subscriber capture
# ---------------------------------------------------------------------------

@dataclass
class Captured:
    topic:   str
    payload: bytes


class Subscriber:
    """Background paho-mqtt subscriber that records every message on msh/#."""

    def __init__(self, host: str, port: int):
        self._host = host
        self._port = port
        self._messages: list[Captured] = []
        self._lock = threading.Lock()
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="floodgate-test-driver-sub",
        )
        self._client.on_message = self._on_message

    def _on_message(self, _client, _userdata, msg):
        with self._lock:
            self._messages.append(Captured(topic=msg.topic, payload=bytes(msg.payload)))

    def start(self):
        self._client.connect(self._host, self._port, keepalive=30)
        self._client.subscribe("msh/#", qos=0)
        self._client.loop_start()
        # Give EMQX a moment to register the subscription before we publish.
        time.sleep(0.5)

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()

    def snapshot(self) -> list[Captured]:
        with self._lock:
            return list(self._messages)


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------

class Publisher:
    def __init__(self, host: str, port: int):
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="floodgate-test-driver-pub",
        )
        self._client.connect(host, port, keepalive=30)
        self._client.loop_start()

    def publish(self, topic: str, payload: bytes):
        info = self._client.publish(topic, payload=payload, qos=1)
        info.wait_for_publish(timeout=5.0)

    def close(self):
        self._client.loop_stop()
        self._client.disconnect()


# ---------------------------------------------------------------------------
# Crypto / packet builders
# ---------------------------------------------------------------------------

def _build_nonce(packet_id: int, from_node: int) -> bytes:
    return (
        (packet_id & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
        + (from_node & 0xFFFFFFFF).to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
    )


def encrypt(plaintext: bytes, key: bytes, packet_id: int, from_node: int) -> bytes:
    nonce = _build_nonce(packet_id, from_node)
    return Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor().update(plaintext)


def build_envelope(
    *,
    channel:   str,
    portnum:   int,
    payload:   bytes,
    packet_id: int,
    from_node: int,
    to_node:   int       = 0xFFFFFFFF,
    hop_limit: int       = 3,
    hop_start: int       = 3,
    key:       bytes     = DEFAULT_KEY,
) -> bytes:
    """Build a Meshtastic ServiceEnvelope wrapping an encrypted Data message."""
    data = mesh_pb2.Data()
    data.portnum = portnum
    data.payload = payload
    encrypted = encrypt(data.SerializeToString(), key=key,
                        packet_id=packet_id, from_node=from_node)

    pkt = mesh_pb2.MeshPacket()
    setattr(pkt, "from", from_node)   # 'from' is a Python keyword
    pkt.to        = to_node
    pkt.id        = packet_id
    pkt.hop_limit = hop_limit
    pkt.hop_start = hop_start
    pkt.encrypted = encrypted

    env = mqtt_pb2.ServiceEnvelope()
    env.packet.CopyFrom(pkt)
    env.channel_id  = channel
    env.gateway_id  = "!00000001"
    return env.SerializeToString()


def topic_for(channel: str, *, gateway: str = "!00000001") -> str:
    return f"msh/US/2/e/{channel}/{gateway}"


# ---------------------------------------------------------------------------
# Health-stats reader
# ---------------------------------------------------------------------------

def health_stats() -> dict:
    resp = requests.get(FLOODGATE_HEALTH, timeout=5)
    resp.raise_for_status()
    return resp.json()["stats"]


# ---------------------------------------------------------------------------
# Envelope inspection helpers (used by every test case to read the delivered
# bytes back out of the subscriber's capture buffer).
# ---------------------------------------------------------------------------

def _parse_hop_limit(payload: bytes) -> int | None:
    """Return MeshPacket.hop_limit from a serialized ServiceEnvelope, or None."""
    try:
        env = mqtt_pb2.ServiceEnvelope()
        env.ParseFromString(payload)
        return env.packet.hop_limit if env.HasField("packet") else None
    except Exception:
        return None


def _parse_hop_start(payload: bytes) -> int | None:
    """Return MeshPacket.hop_start from a serialized ServiceEnvelope, or None."""
    try:
        env = mqtt_pb2.ServiceEnvelope()
        env.ParseFromString(payload)
        return env.packet.hop_start if env.HasField("packet") else None
    except Exception:
        return None


def _packet_id_of(payload: bytes) -> int | None:
    try:
        env = mqtt_pb2.ServiceEnvelope()
        env.ParseFromString(payload)
        return env.packet.id if env.HasField("packet") else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Test-case orchestration scaffold (cases filled in later tasks)
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    name:   str
    passed: bool = True
    detail: str  = ""

    def line(self) -> str:
        prefix = "PASS" if self.passed else "FAIL"
        return f"{prefix}: {self.name}" + (f" — {self.detail}" if self.detail else "")


def case_zerohop(pub: Publisher, sub: Subscriber) -> Outcome:
    name = "zerohop"
    pre = health_stats()
    pkt_id = 0xA1A1A1A1
    body = build_envelope(
        channel   = "LongFast",
        portnum   = portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        payload   = b"hello-zerohop",
        packet_id = pkt_id,
        from_node = 0xDEADBEEF,
        hop_limit = 3,
        hop_start = 3,
    )
    pub.publish(topic_for("LongFast"), body)
    time.sleep(SETTLE_SECONDS)

    delivered = [m for m in sub.snapshot() if _packet_id_of(m.payload) == pkt_id]
    if not delivered:
        return Outcome(name, False, "no packet with our id was delivered")
    hop = _parse_hop_limit(delivered[-1].payload)
    if hop != 0:
        return Outcome(name, False, f"delivered hop_limit={hop}, expected 0")
    hop_start = _parse_hop_start(delivered[-1].payload)
    if hop_start != 0:
        return Outcome(name, False,
                       f"delivered hop_start={hop_start}, expected 0 (#46)")

    post = health_stats()
    if post.get("zerohop", 0) - pre.get("zerohop", 0) < 1:
        return Outcome(name, False, "stats.zerohop did not increment")
    return Outcome(name)


def case_drop(pub: Publisher, sub: Subscriber) -> Outcome:
    name = "drop"
    pre = health_stats()
    pkt_id = 0xA2A2A2A2
    body = build_envelope(
        channel   = "LongFast",
        portnum   = portnums_pb2.PortNum.RANGE_TEST_APP,
        payload   = b"flood",
        packet_id = pkt_id,
        from_node = 0xDEADBEEF,
        hop_limit = 3,
    )
    pub.publish(topic_for("LongFast"), body)
    time.sleep(SETTLE_SECONDS)

    delivered = [m for m in sub.snapshot() if _packet_id_of(m.payload) == pkt_id]
    if delivered:
        return Outcome(name, False,
                       f"packet was delivered to subscriber ({len(delivered)} times); "
                       "drop should have denied it")

    post = health_stats()
    if post.get("dropped", 0) - pre.get("dropped", 0) < 1:
        return Outcome(name, False, "stats.dropped did not increment")
    return Outcome(name)


def case_passthru(pub: Publisher, sub: Subscriber) -> Outcome:
    """Channel NOT in zerohop_channels — packet must transit unchanged."""
    name = "passthru"
    pre = health_stats()
    pkt_id = 0xA3A3A3A3
    body = build_envelope(
        channel   = "PrivateClear",
        portnum   = portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        payload   = b"hello-private",
        packet_id = pkt_id,
        from_node = 0xDEADBEEF,
        hop_limit = 3,
        key       = DEFAULT_KEY,
    )
    pub.publish(topic_for("PrivateClear"), body)
    time.sleep(SETTLE_SECONDS)

    delivered = [m for m in sub.snapshot() if _packet_id_of(m.payload) == pkt_id]
    if not delivered:
        return Outcome(name, False, "packet was not delivered to subscriber")
    if delivered[-1].payload != body:
        return Outcome(name, False,
                       "delivered payload was modified; passthru must be byte-identical")
    hop = _parse_hop_limit(delivered[-1].payload)
    if hop != 3:
        return Outcome(name, False, f"hop_limit={hop}, expected 3 (no zerohop on this channel)")

    post = health_stats()
    if post.get("passthru", 0) - pre.get("passthru", 0) < 1:
        return Outcome(name, False, "stats.passthru did not increment")
    return Outcome(name)


def case_noop(pub: Publisher, sub: Subscriber) -> Outcome:
    """Already hop_limit=0 on a zerohop channel — delivered unchanged, counted as noop."""
    name = "noop"
    pre = health_stats()
    pkt_id = 0xA4A4A4A4
    body = build_envelope(
        channel   = "LongFast",
        portnum   = portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        payload   = b"already-zero",
        packet_id = pkt_id,
        from_node = 0xDEADBEEF,
        hop_limit = 0,
        hop_start = 3,
    )
    pub.publish(topic_for("LongFast"), body)
    time.sleep(SETTLE_SECONDS)

    delivered = [m for m in sub.snapshot() if _packet_id_of(m.payload) == pkt_id]
    if not delivered:
        return Outcome(name, False, "packet was not delivered to subscriber")
    if delivered[-1].payload != body:
        return Outcome(name, False, "noop delivered payload should be byte-identical")

    post = health_stats()
    if post.get("noop", 0) - pre.get("noop", 0) < 1:
        return Outcome(name, False, "stats.noop did not increment")
    return Outcome(name)


def case_custom_key_passthru(pub: Publisher, sub: Subscriber) -> Outcome:
    """Channel NOT in zerohop_channels, encrypted with a key floodgate doesn't have.

    floodgate cannot decrypt the inner Data, so it cannot read the portnum.
    The drop filter must therefore NOT fire even if drop_portnums would
    otherwise match. Because the channel is not in zerohop_channels, the
    packet is delivered byte-identically.
    """
    name = "custom-key-passthru"
    pre = health_stats()
    pkt_id = 0xA5A5A5A5
    body = build_envelope(
        channel   = "PrivateNet",
        portnum   = portnums_pb2.PortNum.RANGE_TEST_APP,  # would match drop_portnums if readable
        payload   = b"opaque",
        packet_id = pkt_id,
        from_node = 0xDEADBEEF,
        hop_limit = 3,
        key       = CUSTOM_KEY,
    )
    pub.publish(topic_for("PrivateNet"), body)
    time.sleep(SETTLE_SECONDS)

    delivered = [m for m in sub.snapshot() if _packet_id_of(m.payload) == pkt_id]
    if not delivered:
        return Outcome(name, False, "packet was not delivered to subscriber")
    if delivered[-1].payload != body:
        return Outcome(name, False,
                       "delivered payload was modified; passthru must be byte-identical")

    post = health_stats()
    if post.get("passthru", 0) - pre.get("passthru", 0) < 1:
        return Outcome(name, False, "stats.passthru did not increment")
    if post.get("dropped", 0) - pre.get("dropped", 0) > 0:
        return Outcome(name, False,
                       "stats.dropped incremented; drop must not fire on unreadable portnum")
    return Outcome(name)


def case_hook_down_fails_open(pub: Publisher, sub: Subscriber) -> Outcome:
    """floodgate unreachable: the packet is delivered UNMODIFIED, not denied.

    This is the case the deployment docs got backwards, so it is worth stating
    precisely. The ExHook is registered `failed_action: deny`, which reads like
    "if floodgate is down, refuse the publish". It does not behave that way for
    `message.publish`. When the gRPC server is unreachable the call *raises*
    inside the hook, and `emqx_hooks:safe_execute/2` catches the exception and
    continues the fold with the original message. `failed_action` is never
    consulted, because that path handles a call that returned a failure — not
    one that blew up.

    The operational consequence is the opposite of what "deny" suggests: a
    floodgate outage does not take the broker down, it silently switches the
    anti-flood protection off. Packets keep flowing with `hop_limit` intact, so
    the mesh reverts to full flooding — exactly what floodgate exists to stop —
    and floodgate logs nothing, because floodgate is not running.

    Asserting delivery alone would pass even if EMQX zeroed the hops some other
    way, so this asserts the hop fields are *untouched*. That is the difference
    between "traffic still flows" and "protection is off".
    """
    name = "hook-down-fails-open"
    before = exhook_failed_count()
    pkt_id = 0x0FF0DEAD
    body = build_envelope(
        channel   = "LongFast",          # in zerohop_channels: would be zeroed if floodgate were up
        portnum   = portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        payload   = b"hook is down",
        packet_id = pkt_id,
        from_node = 0x0BADCAFE,
        hop_limit = 3,
        hop_start = 3,
    )
    pub.publish(topic_for("LongFast"), body)
    # The hook has to fail before EMQX moves on. A dead server refuses
    # immediately, but a *hung* one burns the full request_timeout first.
    time.sleep(HOOK_FAILURE_SETTLE_SECONDS)

    delivered = [m for m in sub.snapshot() if _packet_id_of(m.payload) == pkt_id]
    if not delivered:
        return Outcome(name, False,
                       "packet was NOT delivered — EMQX now fails closed; the "
                       "blast radius of a floodgate outage has changed")
    got = delivered[-1].payload
    if _parse_hop_limit(got) != 3 or _parse_hop_start(got) != 3:
        return Outcome(name, False,
                       f"hop fields were modified with floodgate down "
                       f"(hop_limit={_parse_hop_limit(got)}, hop_start={_parse_hop_start(got)})")
    if got != body:
        return Outcome(name, False, "delivered payload differs from what was published")

    after = exhook_failed_count()
    if after is None or before is None:
        return Outcome(name, False, "could not read exhook metrics from EMQX REST")
    if after <= before:
        return Outcome(name, False,
                       f"exhook metrics.failed did not increment ({before} -> {after}); "
                       "the outage would be invisible to monitoring")
    return Outcome(name)


def case_recovery_after_hook_down(pub: Publisher, sub: Subscriber) -> Outcome:
    """floodgate back up: zero-hopping resumes without re-registering the hook.

    EMQX's `auto_reconnect` is what makes an outage self-healing. If this fails
    while `hook-down-fails-open` passed, an outage is permanent until someone
    re-registers the hook by hand — a much worse failure than the outage.
    """
    name = "recovery-after-hook-down"
    pkt_id = 0x0FF0BEEF
    body = build_envelope(
        channel   = "LongFast",
        portnum   = portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        payload   = b"hook is back",
        packet_id = pkt_id,
        from_node = 0x0BADCAFE,
        hop_limit = 3,
        hop_start = 3,
    )
    pub.publish(topic_for("LongFast"), body)
    time.sleep(SETTLE_SECONDS)

    delivered = [m for m in sub.snapshot() if _packet_id_of(m.payload) == pkt_id]
    if not delivered:
        return Outcome(name, False, "packet was not delivered after floodgate recovered")
    got = delivered[-1].payload
    if _parse_hop_limit(got) != 0 or _parse_hop_start(got) != 0:
        return Outcome(name, False,
                       f"zerohop did not resume (hop_limit={_parse_hop_limit(got)}, "
                       f"hop_start={_parse_hop_start(got)}); hook did not auto-reconnect")
    return Outcome(name)


CASE_SETS: dict[str, list] = {
    # Steady state: floodgate healthy for the whole run.
    "default": [
        case_zerohop,
        case_drop,
        case_passthru,
        case_noop,
        case_custom_key_passthru,
    ],
    # Run by scripts/run-integration.sh with floodgate deliberately stopped.
    "hook-down": [case_hook_down_fails_open],
    # Run after floodgate is started again.
    "recovery": [case_recovery_after_hook_down],
}


def run_all() -> int:
    case_set = os.environ.get("CASE_SET", "default")
    try:
        cases = CASE_SETS[case_set]
    except KeyError:
        print(f"unknown CASE_SET {case_set!r}; expected one of {sorted(CASE_SETS)}",
              file=sys.stderr, flush=True)
        return 2

    sub = Subscriber(EMQX_HOST, EMQX_PORT)
    sub.start()
    pub = Publisher(EMQX_HOST, EMQX_PORT)
    try:
        outcomes: list[Outcome] = [case(pub, sub) for case in cases]
        for o in outcomes:
            print(o.line(), flush=True)
        failed = [o for o in outcomes if not o.passed]
        print(f"\n{len(outcomes) - len(failed)}/{len(outcomes)} cases passed", flush=True)
        return 1 if failed else 0
    finally:
        pub.close()
        sub.stop()


if __name__ == "__main__":
    sys.exit(run_all())
