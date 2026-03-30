# floodgate

Zero-hop MQTT anti-flood service for Meshtastic — intercepts MQTT PUBLISH events via EMQX ExHook (gRPC) and sets `MeshPacket.hop_limit=0` in-flight before delivery to subscribers.

---

## The Problem

Meshtastic gateways upload received LoRa packets to MQTT and, if subscribed, also download MQTT packets back to LoRa. Each downloaded packet retains its original `hop_limit` — so when a gateway re-broadcasts it, nearby nodes see it as a fresh packet and rebroadcast it again. On a busy channel, this cascades into RF saturation.

The [public Meshtastic MQTT server](https://meshtastic.org/docs/software/integrations/mqtt/) prevents this by zeroing `hop_limit` on packets before delivering them to subscribers. Self-hosted EMQX brokers have no built-in equivalent — that's what floodgate provides.

---

## How It Works

```
Gateway (LoRa uplink)
       │  PUBLISH msh/…/e/…
       ▼
     EMQX
       │  ExHook: OnMessagePublish
       ▼
  floodgate (gRPC)
       │  Decodes ServiceEnvelope
       │  Sets MeshPacket.hop_limit = 0
       │  Re-encodes → returns modified payload
       ▼
     EMQX
       │  PUBLISH (modified)
       ▼
 Subscribers (other gateways)
       │  hop_limit=0 → no LoRa rebroadcast
       ▼
    (silence)
```

floodgate operates as an [EMQX ExHook](https://www.emqx.io/docs/en/latest/extensions/exhook.html) — a gRPC server that EMQX calls synchronously for each PUBLISH event. This is different from a standard MQTT client subscriber: it modifies the payload **in-flight** before any subscriber receives it, so zero-hopping is transparent and applies to all subscribers unconditionally.

Protobuf (`/e/`) and JSON (`/json/`) topic formats are both handled.

---

## Packet Processing

Each message is classified into one of four outcomes:

| Outcome | Trigger | What subscribers receive |
|---------|---------|--------------------------|
| `[ZEROHOP]` | Channel matches policy and `hop_limit > 0` | Modified packet: `hop_limit=0`, `hop_start` preserved |
| `[NOOP]` | Channel matches policy and `hop_limit` already `0` | Original packet unchanged |
| `[PASSTHRU]` | Channel exempt by policy | Original packet unchanged |
| `[WARN]` | Payload could not be parsed | Original packet unchanged |

---

## Packet Header Example

floodgate zeroes `hop_limit` while preserving `hop_start`, so the number of hops a packet
_could_ have taken is still available for observability. The packet is otherwise untouched.

```
Before (gateway uplink):             After (floodgate → subscribers):

packet {                              packet {
  id: 3827461829                        id: 3827461829
  from: 0xa2e1a8c4                      from: 0xa2e1a8c4
  to:   0xffffffff  (broadcast)         to:   0xffffffff
  hop_limit: 3                          hop_limit: 0   ← zeroed
  hop_start: 3                          hop_start: 3   ← preserved
  via_mqtt:  false                      via_mqtt:  false
}                                     }
```

---

## Channel Policy

floodgate uses a **channel policy** to decide which channels to zero-hop. Channels are identified by name (the `channel_id` field in the MQTT topic, e.g. `LongFast`).

### Whitelist mode (default)

Zero-hop **everything except** the named channels. An empty whitelist means zero-hop all channels.

```yaml
channel_policy: whitelist
channel_whitelist:
  - MyPrivateChannel   # packets on this channel pass through unmodified
```

### Blacklist mode

Zero-hop **only** the named channels. Everything else passes through.

```yaml
channel_policy: blacklist
channel_blacklist:
  - LongFast
  - LongModerate
  - MediumFast
  - MediumSlow
  - ShortFast
  - ShortSlow
```

---

## Quick Start (Docker Compose)

```bash
git clone https://github.com/eric-becker/floodgate
cd floodgate
docker compose up --build -d
```

Then register the ExHook in EMQX (see the curl command at the top of [docker-compose.yaml](docker-compose.yaml)).

---

## Manual Setup

**Prerequisites:** Python 3.11+, `protoc`, `grpc_tools`

```bash
git clone https://github.com/eric-becker/floodgate
cd floodgate

# Download Meshtastic protobufs (not bundled — Apache 2.0)
./scripts/download_protobufs.sh

# Generate Python stubs
./scripts/generate_protos.sh

# Install
pip install -e ".[dev]"

# Run
floodgate --config config.yaml
floodgate --config config.yaml -v   # DEBUG per-message logging
```

---

## Configuration Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `channel_policy` | string | `whitelist` | `whitelist`: zero-hop all except listed. `blacklist`: zero-hop only listed. |
| `channel_whitelist` | list | `[]` | Channels exempt from zero-hop (whitelist mode). Empty = zero-hop all. |
| `channel_blacklist` | list | standard presets | Channels to zero-hop (blacklist mode). |
| `topic_filter` | string | `msh/#` | MQTT topic pattern floodgate applies to. |
| `grpc_port` | int | `9000` | Port the gRPC server listens on. |
| `stats_interval_s` | int | `60` | Seconds between stats log lines. `0` disables. |
| `log_level` | string | `INFO` | `DEBUG` enables per-message outcome logs. |

See [config.yaml](config.yaml) for a fully annotated example.

---

## Deployment

### Docker Compose

`docker-compose.yaml` starts floodgate alongside EMQX OSS. After startup, register the ExHook
via the EMQX REST API (curl command in the compose file header). Config is mounted at
`/app/config.yaml`.

### Kubernetes

Manifests in [k8s/](k8s/) — a Deployment, Service, and ConfigMap. Adjust the image tag and
ConfigMap data to match your environment, then apply:

```bash
kubectl apply -f k8s/
```

After applying, register the ExHook in EMQX pointing to `http://floodgate:9000`.

---

## Development

```bash
# Clone and install with dev dependencies
pip install -e ".[dev]"

# Run tests (no protobufs required — protobuf imports are mocked)
pytest tests/ -q
```

Tests cover: topic parsing, protobuf zerohop, JSON zerohop, channel policy (whitelist/blacklist),
`_peek_meta` (protobuf and JSON), passthru logging, and stats counting.

---

## Observability

### Log levels

| Level | Output |
|-------|--------|
| `INFO` | Startup, ExHook registration, periodic stats |
| `DEBUG` | Per-message outcome lines (see below) |

### Per-message log format (DEBUG)

```
[ZEROHOP] topic=msh/2/e/LongFast/!a2e1a8c4  channel=LongFast  id=3827461829  from=0xa2e1a8c4  to=0xffffffff  hop_start=3
[NOOP]    topic=msh/2/e/LongFast/!a2e1a8c4  channel=LongFast  id=3827461829  from=0xa2e1a8c4  hop_start=0
[PASSTHRU] topic=msh/2/e/MyPrivate/!a2e1a8c4  channel=MyPrivate  id=1234567890
[WARN]    topic=msh/2/e/LongFast/!a2e1a8c4  channel=LongFast  error=Failed to decode protobuf
```

### Periodic stats (INFO)

```
[STATS] zerohop=142  noop=3  passthru=0  warn=0  (last 60s)
```

---

## License

GPL v3.0 — see [LICENSE](LICENSE).

The EMQX ExHook proto (`proto/emqx/exhook.proto`) is Apache 2.0 © EMQ Technologies.
Meshtastic protobufs (downloaded separately, not bundled) are Apache 2.0 © Meshtastic LLC.
GPL v3.0 is compatible with Apache 2.0 for these use cases.
