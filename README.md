# floodgate

Zero-hop MQTT anti-flood service for self-hosted [EMQX](https://www.emqx.io/) brokers serving [Meshtastic](https://meshtastic.org/) networks. Intercepts MQTT PUBLISH events via [EMQX ExHook](https://www.emqx.io/docs/en/latest/extensions/exhook.html) (gRPC) and sets `MeshPacket.hop_limit=0` in-flight before delivery to subscribers, preventing LoRa rebroadcast floods when gateways downlink MQTT packets.

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
     EMQX  →  Subscribers
```

Unlike a standard MQTT subscriber, floodgate modifies payloads in-flight — all subscribers receive the zeroed packet transparently. Both protobuf (`/e/`) and JSON (`/json/`) topic formats are supported.

```
Before:  hop_limit: 3  hop_start: 3
After:   hop_limit: 0  hop_start: 3  ← hop_start preserved for observability
```

Each message produces one outcome:

| Outcome | Condition |
|---------|-----------|
| `[ZEROHOP]` | Channel matched policy, `hop_limit > 0` — zeroed |
| `[NOOP]` | Channel matched policy, already `hop_limit=0` |
| `[PASSTHRU]` | Channel exempt by policy — unchanged |
| `[WARN]` | Payload parse failure — unchanged |

## Quick Start

```bash
git clone https://github.com/eric-becker/floodgate
cd floodgate
docker compose up --build -d
```

Register the ExHook in EMQX after startup (curl command in [docker-compose.yaml](docker-compose.yaml) header).

## Installation

**Prerequisites:** Python 3.11+, `protoc`, `grpc_tools`

```bash
./scripts/download_protobufs.sh   # fetch Meshtastic protobufs (Apache 2.0, not bundled)
./scripts/generate_protos.sh      # generate Python stubs
pip install -e ".[dev]"

floodgate --config config.yaml
floodgate --config config.yaml -v  # DEBUG per-message logging
```

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `channel_policy` | `whitelist` | `whitelist`: zero-hop all except listed. `blacklist`: zero-hop only listed. |
| `channel_whitelist` | `[]` | Channels exempt from zero-hop (whitelist mode). Empty = zero-hop all. |
| `channel_blacklist` | standard presets | Channels to zero-hop (blacklist mode). |
| `topic_filter` | `msh/#` | MQTT topic pattern to apply. |
| `grpc_port` | `9000` | gRPC listen port. |
| `stats_interval_s` | `60` | Stats log interval in seconds. `0` disables. |
| `log_level` | `INFO` | `DEBUG` enables per-message outcome logs. |

See [config.yaml](config.yaml) for a fully annotated example.

**Whitelist mode** — zero-hop everything except named channels:
```yaml
channel_policy: whitelist
channel_whitelist:
  - MyPrivateChannel
```

**Blacklist mode** — zero-hop only named channels:
```yaml
channel_policy: blacklist
channel_blacklist:
  - LongFast
  - LongModerate
```

## Deployment

**Docker Compose** — see [docker-compose.yaml](docker-compose.yaml). Runs floodgate alongside EMQX OSS.

**Kubernetes** — see [k8s/](k8s/). Apply manifests and register the ExHook at `http://floodgate:9000`.

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -q   # no protobufs required — protobuf imports are mocked
```

**Log format (DEBUG):**
```
[ZEROHOP]  topic=msh/2/e/LongFast/!a2e1a8c4  channel=LongFast  id=3827461829  hop_start=3
[PASSTHRU] topic=msh/2/e/MyPrivate/!a2e1a8c4  channel=MyPrivate  id=1234567890
[STATS]    zerohop=142  noop=3  passthru=0  warn=0  (last 60s)
```

## License

GPL v3.0 — see [LICENSE](LICENSE).
EMQX ExHook proto and Meshtastic protobufs are Apache 2.0 (not bundled).
