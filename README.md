# floodgate

Zero-hop MQTT anti-flood service for self-hosted [EMQX](https://www.emqx.io/) brokers serving [Meshtastic](https://meshtastic.org/) networks. Intercepts MQTT PUBLISH events via [EMQX ExHook](https://www.emqx.io/docs/en/latest/extensions/exhook.html) (gRPC) and zeros both `MeshPacket.hop_limit` and `hop_start` in-flight before delivery to subscribers, preventing LoRa rebroadcast floods when gateways downlink MQTT packets.

Tested against EMQX 6.2.0 (ExHook v3 proto, EMQX 5.9.0+).

## Why

Meshtastic's official public broker enforces a [zero-hop policy](https://meshtastic.org/docs/software/integrations/mqtt/#zero-hop-policy): packets delivered from the broker to gateway nodes are zeroed before downlink so they don't rebroadcast across the local LoRa mesh. This prevents internet-scale MQTT traffic from flooding regional radio networks.

**Private brokers don't enforce this by default.** The Meshtastic docs [explicitly warn](https://meshtastic.org/docs/software/integrations/mqtt/#using-private-brokers) that using default encryption keys on private brokers is discouraged because they lack the zero-hop policy enforcement of the public broker — packets downlinked from a private broker can flood the local mesh at full hop count.

floodgate fills this gap. It runs alongside your self-hosted EMQX and enforces zero-hop in-flight via the [ExHook gRPC interface](https://www.emqx.io/docs/en/latest/extensions/exhook.html), giving your private broker the same protection the public broker provides — without requiring any changes to your clients, gateways, or EMQX configuration beyond registering the ExHook.

## How It Works

```
Gateway → EMQX → [ExHook gRPC] → floodgate → modified payload → EMQX → Subscribers
```

Unlike a standard MQTT subscriber, floodgate modifies payloads **in-flight** — all subscribers receive the zeroed packet transparently. Meshtastic gateways use the protobuf (`/e/`) topic for LoRa downlink. The JSON (`/json/`) topic is a human-readable mirror that some clients publish alongside for monitoring tools like MQTT Explorer — floodgate zero-hops both for consistency.

```
Before:  hop_limit: 3  hop_start: 3
After:   hop_limit: 0  hop_start: 0  ← both zeroed so receivers don't render ghost hops in traceroute
```

Original sender values remain visible in floodgate's per-message log records (extracted before modification). Zeroing `hop_start` alongside `hop_limit` prevents receiving firmware from computing `hopsTaken = hop_start - hop_limit > 0` and padding `RouteDiscovery.route[]` with `0xFFFFFFFF` sentinels (rendered as "Meshtastic ffff (ffff)" in client apps).

See [Meshtastic Mesh Algorithm](https://meshtastic.org/docs/overview/mesh-algo/) for details on `hop_limit` and `hop_start`.

Each message produces one outcome:

| Outcome | Condition |
|---------|-----------|
| `dropped` | Matched the drop filter — denied entirely; EMQX never delivers it |
| `zerohop` | Channel in `zerohop_channels`, `hop_limit > 0` — zeroed and delivered |
| `noop` | Channel in `zerohop_channels`, already `hop_limit=0` — delivered unchanged |
| `passthru` | Channel not in `zerohop_channels` (or zerohop disabled) — delivered unchanged |
| `warn` | Payload parse failure — delivered unchanged |
| `skipped` | Non-packet topic (map report, stat, etc.) — silently ignored |

The drop filter runs **before** zerohop. A packet matching both is reported as `dropped`.

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
    "auto_reconnect": "5s",
    "failed_action": "deny"
  }'
```

If floodgate is on the same Docker network as EMQX, use the container name instead of an IP:
`"url": "http://floodgate:9000"`

See [ExHook failure policy](#exhook-failure-policy) for why `deny` is recommended.

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

After startup, register the ExHook per the [Deployment](#deployment) instructions above.

### Kubernetes (Helm)

```bash
helm install floodgate ./charts/floodgate -n floodgate --create-namespace
```

See [charts/floodgate/](charts/floodgate/) for the full values reference. The chart renders a Deployment with rolling updates, a ClusterIP Service, ConfigMap, ServiceAccount, and an optional PodDisruptionBudget; ConfigMap changes trigger automatic rolling restarts via a checksum annotation. Register the ExHook at `http://floodgate.floodgate.svc:9000` after install.

The flat manifests in [k8s/](k8s/) are still present for `kubectl apply -f` users but are deprecated in favour of the chart.

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
| `zerohop_enabled` | `true` | Master switch for the zero-hop modifier. |
| `zerohop_channels` | 8 standard presets | Channels whose packets get `hop_limit` and `hop_start` zeroed. |
| `drop_enabled` | `false` | Master switch for the drop filter (deny entirely). |
| `drop_channels` | `"zerohop_channels"` | Channels on which the drop filter runs. List of channels, the literal string `"zerohop_channels"` to inherit, or `null` for all channels. |
| `drop_portnums` | `[]` | Meshtastic portnums (proto enum names like `RANGE_TEST_APP`) to drop. |
| `topic_filter` | `msh/#` | MQTT topic pattern to apply. |
| `grpc_port` | `9000` | gRPC listen port. |
| `grpc_max_workers` | `16` | Size of the gRPC server thread pool. Match or exceed the broker's ExHook `pool_size` — see [Sizing the thread pool](#sizing-the-thread-pool). |
| `health_port` | `8080` | HTTP health check port. `GET /health` returns `{"status":"ok","stats":{...}}`. |
| `stats_interval_s` | `60` | Stats log interval in seconds. |
| `stats_log` | `true` | Log periodic stats summaries. Set `false` to disable. |
| `log_level` | `INFO` | `INFO` shows per-message outcomes. `DEBUG` adds verbose internals. |
| `log_format` | `text` | `text` for human-readable output, `json` for Loki/Grafana structured logging. Can also be set via `FLOODGATE_LOG_FORMAT` env var. |

> **Migrating from a pre-rename config?** The keys `channel_policy`, `channel_blacklist`, and `channel_whitelist` were removed. floodgate now refuses to start if they appear in `config.yaml` and prints the new equivalents. Update your config and restart.

### Zerohop

The default zerohop list contains the eight standard Meshtastic public channel presets:
```yaml
zerohop_enabled: true
zerohop_channels:
  - "LongTurbo"
  - "LongFast"
  - "LongModerate"
  - "MediumFast"
  - "MediumSlow"
  - "ShortFast"
  - "ShortSlow"
  - "ShortTurbo"
```

To zero-hop every channel a gateway forwards (maximum enforcement), pass them all in `zerohop_channels`. To leave a private channel rebroadcasting normally, omit it from the list. To turn the modifier off entirely (e.g. during a maintenance window), set `zerohop_enabled: false`.

### Drop

The drop filter denies a publish entirely — EMQX does not deliver it to subscribers, so it doesn't appear in MQTT consumers, dashboards, or chat logs. Drop runs *before* zerohop; a dropped packet never reaches the zerohop check.

The motivating use case is portnum traffic that floods the standard public channels and isn't useful in MQTT downstream — for example, `RANGE_TEST_APP`:
```yaml
drop_enabled: true
drop_channels: "zerohop_channels"   # same scope as zerohop
drop_portnums:
  - "RANGE_TEST_APP"
```

`drop_channels` accepts:

- the literal string `"zerohop_channels"` to reuse the same list as `zerohop_channels` (default — keeps drop scope aligned with zerohop scope without duplication),
- an explicit list of channel names, e.g. `["LongFast", "MediumFast"]`,
- `null` (or an empty list) to apply on all channels.

> **Encryption constraint.** `drop_portnums` only acts on packets whose portnum floodgate can read. For protobuf (`/e/`) topics that means channels using the default Meshtastic encryption key (`AQ==`); custom-keyed channels are unreadable and therefore always delivered. JSON (`/json/`) topics expose the portnum directly and have no such limitation.

> **Publisher behavior.** When floodgate denies a publish, EMQX still sends a normal PUBACK to the publisher — there's no protocol-level signal back to the gateway that its message was filtered. This is a property of the EMQX ExHook deny mechanism, not floodgate. Operators monitoring publisher-side success counters won't see drops; the floodgate `dropped` counter and per-message log lines are the source of truth.

### ExHook failure policy

EMQX's `failed_action` is documented as controlling what happens to MQTT messages when floodgate
is unreachable (crash, restart, upgrade). **For `message.publish` it does not do what its name
suggests — floodgate fails _open_ either way.**

| `failed_action` | Floodgate down | Observed |
|-----------------|----------------|----------|
| `deny` | Messages **delivered at full `hop_limit`** | Mesh floods |
| `ignore` | Messages delivered at full `hop_limit` | Mesh floods |

Verified against EMQX 6.2.0 for both failure modes — a stopped server (connection refused) and a
hung one (call exceeds `request_timeout`). In both, `metrics.failed` incremented and the message
was still delivered, byte-identical, with `hop_limit` untouched. EMQX's own log shows why:

```
exhook_call_exception ... failed_action => deny
  emqx_exhook:call_fold/5 → emqx_exhook_handler:on_message_publish/1 → emqx_hooks:safe_execute/2
```

An unreachable server makes the gRPC call **raise**. `emqx_hooks:safe_execute/2` catches the
exception and continues the hook fold with the original message, so `failed_action` is never
consulted — that setting handles a call that *returned* a failure, not one that blew up.

**What this means operationally.** A floodgate outage will not interrupt your broker, and it will
not drop packets. It silently switches the anti-flood protection off: traffic keeps flowing at
full `hop_limit` and the mesh reverts to exactly the flooding floodgate exists to prevent.
Nothing in floodgate's logs will say so, because floodgate isn't running.

**Monitor `metrics.failed`**, which is the only place the outage is visible:

```bash
curl -s "$EMQX/api/v5/exhooks/floodgate" -H "Authorization: Bearer $TOKEN" \
  | jq '{failed: .metrics.failed, status: [.node_status[].status]}'
```

Alert on it increasing, or on EMQX's `exhook_call_exception` error log. Publisher-side counters
and subscriber-side delivery both look completely normal during an outage.

Regression-tested by the `hook-down-fails-open` and `recovery-after-hook-down` integration cases,
so if a future EMQX release starts failing closed, CI will say so.

### Sizing the thread pool

EMQX drives the ExHook with a connection pool (`pool_size`, default `8`). floodgate serves those
calls from a fixed thread pool of `grpc_max_workers` (default `16`). **If floodgate's pool is
smaller than the broker's, calls queue behind busy workers.**

A call that takes longer than the broker's `request_timeout` is counted as a failure — and per the
section above, a failed call means the message is **delivered unmodified**, regardless of
`failed_action`. So an undersized pool doesn't cost you packets; it costs you *protection*, one
packet at a time, exactly on the busiest channels where queuing is most likely.

That is harder to notice than a drop. The packet arrives, subscribers see it, and floodgate's own
logs show the call eventually succeeding — while the mesh floods. `metrics.failed` is again the
signal.

Rules of thumb:

- Keep `grpc_max_workers` **>= the broker's exhook `pool_size`**.
- Raising it is cheap (idle threads cost almost nothing) — the default of `16` covers EMQX's
  default of `8` with headroom.
- It is not a throughput dial. Per-message work (protobuf parse, AES decrypt) is CPU-bound and
  serialized by the GIL; more workers prevent head-of-line blocking, they do not add parallelism.

The configured value is logged at startup:
`ExHook gRPC server listening on port 9000  (max_workers=16)`

**Startup ordering:** In Docker Compose, EMQX should `depends_on` floodgate (not the other way around). Floodgate is a gRPC server — it starts first, listens on port 9000, and waits. EMQX connects to it after boot. See [docker-compose.yaml](docker-compose.yaml) for the reference configuration.

**Kubernetes** handles this differently: the Deployment uses a rolling update strategy (`maxUnavailable: 0, maxSurge: 1`) so the new pod starts and passes its readiness probe before the old pod is terminated. EMQX reconnects to the new pod via the Service — zero-downtime upgrades with no message gap.

## Security

### Network exposure

| Port | Purpose | Exposure |
|------|---------|----------|
| `9000` (gRPC) | EMQX ExHook connection | **Internal only.** Bind to your private/cluster network. Never expose publicly. |
| `8080` (HTTP) | Health check `/health` | **Internal only.** Operational stats — restrict to your monitoring network. |

The gRPC connection between EMQX and floodgate is **unencrypted** (no TLS). This is acceptable when both services run in the same Kubernetes namespace or Docker network on a trusted private network. Do not route this port through a public-facing load balancer or ingress.

The Kubernetes manifests in [k8s/](k8s/) use `type: ClusterIP` so neither port is externally reachable by default.

### Container hardening

The production container image runs as `nobody` (UID 65534) with a read-only filesystem, no Linux capabilities, and no privilege escalation. See [Dockerfile](Dockerfile) and the chart's [containerSecurityContext defaults](charts/floodgate/values.yaml).

## Verifying operation

### Health endpoint

floodgate exposes a health check at `GET /health` on port 8080 (configurable via `health_port`). Stats are cumulative lifetime counters that persist across stats reporter intervals:

```bash
curl -s http://localhost:8080/health | jq .
```

```json
{
  "status": "ok",
  "stats": {
    "zerohop": 142,
    "passthru": 3,
    "noop": 0,
    "dropped": 4,
    "skipped": 1050,
    "errors": 0,
    "total": 1199
  }
}
```

### Log output

Per-message outcomes are logged at **INFO** — no special flags required. Field names match [Meshtastic protobuf terminology](https://meshtastic.org/docs/overview/mesh-algo/) (`hop_limit`, `hop_start`, `from`, `to`, `id`).

**Text mode** (default):
```
2026-04-01 12:00:01 INFO     [floodgate.zerohop] [ZEROHOP] topic=msh/US/2/e/LongFast/!a2e1a8c4 channel=LongFast encoding=e id=3827461829 from=!a2e1a8c4 to=!ffffffff hop_limit=3 hop_start=3
2026-04-01 12:00:02 INFO     [floodgate.zerohop] [NOOP] topic=msh/US/2/e/LongFast/!b3c4d5e6 channel=LongFast encoding=e id=2019283746 from=!b3c4d5e6 to=!ffffffff hop_limit=0 hop_start=3
2026-04-01 12:00:08 INFO     [floodgate.zerohop] [DROPPED] topic=msh/US/2/e/LongFast/!c1c2c3c4 channel=LongFast encoding=e portnum=RANGE_TEST_APP id=694258842 from=!c1c2c3c4 to=!ffffffff
2026-04-01 12:00:15 INFO     [floodgate.zerohop] [PASSTHRU] topic=msh/US/2/e/MyPrivate/!a2e1a8c4 channel=MyPrivate encoding=e id=1234567890 from=!a2e1a8c4 to=!ffffffff
2026-04-01 12:01:01 INFO     [floodgate.exhook_server] [STATS] interval_s=60 zerohop=142 passthru=1 noop=0 dropped=4 skipped=1050 errors=0 total=1197
```

**JSON mode** (`log_format: json` or `FLOODGATE_LOG_FORMAT=json`):
```json
{"timestamp":"2026-04-01T12:00:01Z","level":"INFO","name":"floodgate.zerohop","message":"zerohop","event":"message","outcome":"zerohop","topic":"msh/US/2/e/LongFast/!a2e1a8c4","channel":"LongFast","encoding":"e","id":3827461829,"from":"!a2e1a8c4","to":"!ffffffff","hop_limit":3,"hop_start":3,"relay":null,"via_mqtt":null}
```

JSON output is optimized for Loki/Grafana: the `message` field is just the outcome tag, all data is in structured top-level fields. Example LogQL queries:

```
{container="floodgate"} | json | outcome="zerohop"
{container="floodgate"} | json | channel="LongFast"
{container="floodgate"} | json | from="!a2e1a8c4"
sum by (outcome) (count_over_time({container="floodgate"} | json | event="message" [5m]))
```

### Kubernetes

```bash
kubectl logs -n floodgate deploy/floodgate -f
kubectl logs -n floodgate deploy/floodgate --since=5m | grep ZEROHOP | wc -l
```

### Sample packet (before / after)

A `ServiceEnvelope` arriving on topic `msh/US/2/e/LongFast/!a2e1a8c4` — protobuf fields shown for clarity:

**Before** (as published by the uploading gateway):
```
MeshPacket {
  from:       0xa2e1a8c4   (!a2e1a8c4)
  to:         0xffffffff   (broadcast)
  id:         3827461829
  hop_limit:  3            ← will flood the mesh on downlink
  hop_start:  3
  channel:    0
  payload:    <encrypted Data protobuf>
}
```

**After** (returned to EMQX, delivered to all subscribers):
```
MeshPacket {
  from:       0xa2e1a8c4
  to:         0xffffffff
  id:         3827461829
  hop_limit:  0            ← zeroed — gateway will not rebroadcast
  hop_start:  0            ← zeroed so receivers don't render ghost hops in traceroute
  channel:    0
  payload:    <encrypted Data protobuf>   (unchanged)
}
```

The JSON topic mirror (`/json/LongFast/!a2e1a8c4`) is zeroed the same way — `hop_limit`, `hop_start`, and `hops_away` are set to `0` in the JSON object when present.

## Development

```bash
pip install -e ".[dev]"
pytest tests/ --ignore=tests/test_container_smoke.py -q   # no Docker required
pytest tests/ -q   # full suite including container smoke test (requires Docker)
```

**Log output (INFO, text mode):**
```
2026-04-01 14:23:47 INFO     [floodgate.zerohop] [ZEROHOP] topic=msh/US/2/e/LongFast/!a2e1a8c4 channel=LongFast encoding=e id=3827461829 from=!a2e1a8c4 to=!ffffffff hop_limit=3 hop_start=3
2026-04-01 14:23:48 INFO     [floodgate.zerohop] [PASSTHRU] topic=msh/US/2/e/MyPrivate/!a2e1a8c4 channel=MyPrivate encoding=e id=1234567890 from=!a2e1a8c4 to=!ffffffff
2026-04-01 14:24:47 INFO     [floodgate.exhook_server] [STATS] interval_s=60 zerohop=142 passthru=3 noop=0 dropped=2 skipped=1050 errors=0 total=1197
```

## Legal

This project is not affiliated with, endorsed by, or officially associated with the Meshtastic project or Meshtastic LLC. Meshtastic® is a registered trademark of Meshtastic LLC.

floodgate is independent software that interoperates with Meshtastic's MQTT packet format. Use of the Meshtastic name is solely for the purpose of identifying compatibility.

## License

GPL v3.0 — see [LICENSE](LICENSE).
EMQX ExHook proto and Meshtastic protobufs are Apache 2.0 (not bundled).
