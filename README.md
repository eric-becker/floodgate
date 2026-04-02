# floodgate

Zero-hop MQTT anti-flood service for self-hosted [EMQX](https://www.emqx.io/) brokers serving [Meshtastic](https://meshtastic.org/) networks. Intercepts MQTT PUBLISH events via [EMQX ExHook](https://www.emqx.io/docs/en/latest/extensions/exhook.html) (gRPC) and sets `MeshPacket.hop_limit=0` in-flight before delivery to subscribers, preventing LoRa rebroadcast floods when gateways downlink MQTT packets.

## Why

Meshtastic's official public broker enforces a [zero-hop policy](https://meshtastic.org/docs/software/integrations/mqtt/#zero-hop-policy): packets delivered from the broker to gateway nodes are zeroed before downlink so they don't rebroadcast across the local LoRa mesh. This prevents internet-scale MQTT traffic from flooding regional radio networks.

**Private brokers don't enforce this by default.** The Meshtastic docs [explicitly warn](https://meshtastic.org/docs/software/integrations/mqtt/#using-private-brokers) that using default encryption keys on private brokers is discouraged because they lack the zero-hop policy enforcement of the public broker — packets downlinked from a private broker can flood the local mesh at full hop count.

floodgate fills this gap. It runs alongside your self-hosted EMQX and enforces zero-hop in-flight via the [ExHook gRPC interface](https://www.emqx.io/docs/en/latest/extensions/exhook.html), giving your private broker the same protection the public broker provides — without requiring any changes to your clients, gateways, or EMQX configuration beyond registering the ExHook.

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

Unlike a standard MQTT subscriber, floodgate modifies payloads in-flight — all subscribers receive the zeroed packet transparently. Meshtastic gateways use the protobuf (`/e/`) topic for LoRa downlink. The JSON (`/json/`) topic is a human-readable mirror that some clients publish alongside for monitoring tools like MQTT Explorer — floodgate zero-hops both for consistency.

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

## Deployment

### Docker (existing EMQX install)

If you already have EMQX running, run floodgate as a standalone container on the same host:

```bash
git clone https://github.com/eric-becker/floodgate
cd floodgate
cp config.yaml my-config.yaml   # edit to taste
docker build -t floodgate .     # protobufs are downloaded automatically during build
docker run -d \
  --name floodgate \
  --restart unless-stopped \
  -v "$(pwd)/my-config.yaml:/app/config.yaml:ro" \
  -p 9000:9000 \
  floodgate
```

Then register floodgate as an ExHook in EMQX. Get an API token first:

```bash
TOKEN=$(curl -s -X POST http://localhost:18083/api/v5/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"your_password"}' | jq -r .token)
```

Register the ExHook (replace `YOUR_HOST_IP` with the IP floodgate is reachable on from EMQX):

```bash
curl -X POST http://localhost:18083/api/v5/exhooks \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "floodgate",
    "url": "http://YOUR_HOST_IP:9000",
    "auto_reconnect": "60s",
    "failed_action": "ignore"
  }'
```

If floodgate is on the same Docker network as EMQX, use the container name instead of an IP:
`"url": "http://floodgate:9000"`

Verify registration in the EMQX dashboard under **Management → ExHook** or:

```bash
curl -s http://localhost:18083/api/v5/exhooks/floodgate \
  -H "Authorization: Bearer $TOKEN" | jq .status
```

### Docker Compose (floodgate + EMQX together)

```bash
git clone https://github.com/eric-becker/floodgate
cd floodgate
docker compose up --build -d
```

See [docker-compose.yaml](docker-compose.yaml) for the ExHook registration curl command to run after startup.

### Kubernetes

See [k8s/](k8s/) — Deployment, Service, and ConfigMap. Register the ExHook at `http://floodgate:9000` after applying.

### Source install

**Prerequisites:** Python 3.11+, `protoc`, `grpc_tools`

```bash
./scripts/download_protobufs.sh   # fetch Meshtastic protobufs (Apache 2.0, not bundled)
./scripts/generate_protos.sh      # generate Python stubs
pip install -e ".[dev]"

floodgate --config config.yaml
floodgate --config config.yaml -v  # very verbose DEBUG logging
```

## Configuration

See [config.yaml](config.yaml) for a fully annotated example.

| Key | Default | Description |
|-----|---------|-------------|
| `channel_policy` | `blacklist` | See policy docs below. |
| `channel_blacklist` | 8 standard presets | Channels to zero-hop (blacklist mode). |
| `channel_whitelist` | `[]` | Channels exempt from zero-hop (whitelist mode). |
| `topic_filter` | `msh/#` | MQTT topic pattern to apply. |
| `grpc_port` | `9000` | gRPC listen port. |
| `health_port` | `8080` | HTTP health check port. `GET /health` returns `{"status":"ok","stats":{...}}`. |
| `stats_interval_s` | `60` | Stats log interval in seconds. |
| `log_level` | `INFO` | `INFO` shows per-message outcomes. `DEBUG` adds verbose internals. |
| `log_format` | `text` | `text` for human-readable output, `json` for Loki/Grafana structured logging. Can also be set via `FLOODGATE_LOG_FORMAT` env var. |

### Channel policy

**`blacklist` (default)** — zero-hop only the channels named in `channel_blacklist`. All other channels are forwarded unchanged. This is the right choice for most deployments: it targets the standard Meshtastic public presets that flood radio networks, while leaving private or custom channels untouched.

The default `channel_blacklist` contains the eight standard Meshtastic public channel presets:
```yaml
channel_policy: "blacklist"
channel_blacklist:
  - "LongTurbo"
  - "LongFast"
  - "LongModerate"
  - "MediumFast"
  - "MediumSlow"
  - "ShortFast"
  - "ShortSlow"
  - "ShortTurbo"
```

**`whitelist`** — zero-hop ALL channels *except* those named in `channel_whitelist`. Use this for blanket enforcement when you want every channel zeroed with only specific exemptions.

To zero-hop every packet with no exceptions — maximum enforcement — use an empty whitelist:
```yaml
channel_policy: "whitelist"
channel_whitelist: []
```

To exempt specific channels (e.g. a private channel your gateways should rebroadcast normally):
```yaml
channel_policy: "whitelist"
channel_whitelist:
  - "MyPrivateChannel"
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -q   # no protobufs required — protobuf imports are mocked
```

**Log output (INFO, text mode):**
```
2024-03-30 14:23:47 INFO     [floodgate.zerohop] [ZEROHOP]  topic=msh/US/2/e/LongFast/!a2e1a8c4  channel=LongFast  encoding=e  hop 3→0  id=3827461829  from=!a2e1a8c4  to=^all
2024-03-30 14:23:48 INFO     [floodgate.zerohop] [PASSTHRU] topic=msh/US/2/e/MyPrivate/!a2e1a8c4  channel=MyPrivate  id=1234567890
2024-03-30 14:24:47 INFO     [floodgate.exhook_server] Stats [last 60s]  zerohopped=142   passthru=3    noop=0    skipped=1050  errors=0    total=1195
```

## Legal

This project is not affiliated with, endorsed by, or officially associated with the Meshtastic project or Meshtastic LLC. Meshtastic® is a registered trademark of Meshtastic LLC.

floodgate is independent software that interoperates with Meshtastic's MQTT packet format. Use of the Meshtastic name is solely for the purpose of identifying compatibility.

## License

GPL v3.0 — see [LICENSE](LICENSE).
EMQX ExHook proto and Meshtastic protobufs are Apache 2.0 (not bundled).
