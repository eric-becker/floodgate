# floodgate

Zero-hop MQTT anti-flood service for self-hosted [EMQX](https://www.emqx.io/) brokers serving [Meshtastic](https://meshtastic.org/) networks. Intercepts MQTT PUBLISH events via [EMQX ExHook](https://www.emqx.io/docs/en/latest/extensions/exhook.html) (gRPC) and zeros both `MeshPacket.hop_limit` and `hop_start` in-flight before delivery to subscribers, preventing LoRa rebroadcast floods when gateways downlink MQTT packets.

Tested against EMQX 6.2.0 (ExHook v3 proto, EMQX 5.9.0+). CI runs a real Meshtastic firmware node (`meshtasticd`) against floodgate's output, and the zero-hop and drop behavior was verified on hardware (a Station G2 gateway and an nRF52 node on firmware 2.8.1).

## Why

Meshtastic's official public broker enforces a [zero-hop policy](https://meshtastic.org/docs/software/integrations/mqtt/#zero-hop-policy): packets delivered from the broker to gateway nodes are zeroed before downlink so they don't rebroadcast across the local LoRa mesh. This prevents internet-scale MQTT traffic from flooding regional radio networks.

**Private brokers don't enforce this by default.** The Meshtastic docs [explicitly warn](https://meshtastic.org/docs/software/integrations/mqtt/#using-private-brokers) that using default encryption keys on private brokers is discouraged because they lack the zero-hop policy enforcement of the public broker — packets downlinked from a private broker can flood the local mesh at full hop count.

floodgate fills this gap. It runs alongside your self-hosted EMQX and enforces zero-hop in-flight via the [ExHook gRPC interface](https://www.emqx.io/docs/en/latest/extensions/exhook.html), giving your private broker the same protection the public broker provides — without requiring any changes to your clients, gateways, or EMQX configuration beyond registering the ExHook.

The point is selective: zero-hop the public channels, whose statewide noise, telemetry and range tests would swamp a local mesh, and let private or regional channels keep propagating between MQTT and RF. Those are usually low-volume and geographically scoped, and bridging them (emergency channels especially) is only safe once the public-channel traffic is kept off the air.

## How It Works

```
Gateway → EMQX → [ExHook gRPC] → floodgate → modified payload → EMQX → Subscribers
```

Unlike a standard MQTT subscriber, floodgate modifies payloads **in-flight** — all subscribers receive the zeroed packet transparently, including analytics tools. Anything that computes hops as `hop_start - hop_limit` (Malla, for example) shows zero-hopped packets as 0 hops; the sender's original values survive only in floodgate's own log. Meshtastic gateways downlink only the protobuf (`/e/`) topic. The JSON (`/json/`) topic is a human-readable mirror that firmware before 2.8 could publish for monitoring tools — floodgate zero-hops both for consistency.

```
Before:  hop_limit: 3  hop_start: 3
After:   hop_limit: 0  hop_start: 0  ← both zeroed so receivers don't render ghost hops in traceroute
```

Original sender values remain visible in floodgate's per-message log records (extracted before modification). Zeroing `hop_start` alongside `hop_limit` prevents receiving firmware from computing `hopsTaken = hop_start - hop_limit > 0` and padding `RouteDiscovery.route[]` with `0xFFFFFFFF` sentinels (rendered as "Meshtastic ffff (ffff)" in client apps).

What a downlinking gateway then does with each kind of packet (observed on firmware 2.8.1):

| Packet from MQTT | Gateway's own app | Retransmitted on RF |
|---|---|---|
| Zero-hopped channel (`hop_limit: 0`) | Shown, as 0 hops | **No** |
| Any other channel (e.g. `hop_limit: 3`) | Shown | Yes, with `hop_limit` decremented |

So zero-hopped traffic reaches the gateway's own user and stops there; it never reaches RF neighbors.

See [Meshtastic Mesh Algorithm](https://meshtastic.org/docs/overview/mesh-algo/) for details on `hop_limit` and `hop_start`.

Each message produces one outcome:

| Outcome | Condition |
|---------|-----------|
| `dropped` | Matched the drop filter — denied entirely; EMQX never delivers it |
| `zerohop` | Channel in `zerohop_channels`, `hop_limit > 0` — zeroed and delivered |
| `noop` | Channel in `zerohop_channels`, already `hop_limit=0` — delivered unchanged |
| `passthru` | Channel not in `zerohop_channels` (or zerohop disabled) — delivered unchanged |
| `warn` | Payload on a zero-hop channel failed to parse — delivered unchanged, logged at WARNING, counted as `errors` |
| `skipped` | Non-packet topic (map report, stat, etc.) — silently ignored |

The drop filter runs **before** zero-hop. A packet matching both is reported as `dropped`. An unexpected internal error is logged with a traceback, counted as `errors`, and the message is delivered unchanged — floodgate never drops a packet because of its own bug.

## Deployment

### Docker (existing EMQX install)

If you already have EMQX running, run floodgate as a standalone container on the same host. Images are published to GHCR for amd64 and arm64:

```bash
curl -fsSLo my-config.yaml https://raw.githubusercontent.com/eric-becker/floodgate/v0.6.0/config.yaml   # edit to taste
docker run -d \
  --name floodgate \
  --restart unless-stopped \
  -v "$(pwd)/my-config.yaml:/app/config.yaml:ro" \
  -p 127.0.0.1:9000:9000 \
  ghcr.io/eric-becker/floodgate:0.6.0
```

**Pin a version tag, and fetch `config.yaml` from the same tag.** `latest` is rebuilt on every push to `main`, and its digest differs from the tagged build of the same commit. floodgate refuses to start on a config key it doesn't recognize, so a `config.yaml` from `main` can stop an older image from starting. Read the release notes before bumping: config keys have changed between minor versions.

**Port `9000` is unauthenticated gRPC.** Docker-published ports bypass host firewalls such as ufw, so publish it on loopback or a private address, never `0.0.0.0`. If EMQX also runs in Docker, put both on one Docker network and skip `-p` entirely. To build from source instead, `docker build -t floodgate .` fetches the protobufs at their pinned commit during the build.

Then [register the ExHook](#registering-the-exhook).

### Registering the ExHook

floodgate does nothing until EMQX calls it. Register it once per broker. The same steps apply to every install method; only the URL differs.

```bash
EMQX=http://localhost:18083          # EMQX REST API
TOKEN=$(curl -s -X POST "$EMQX/api/v5/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<dashboard password>"}' | jq -r .token)

curl -s -X POST "$EMQX/api/v5/exhooks" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "floodgate",
    "url": "http://<floodgate address>:9000",
    "auto_reconnect": "5s",
    "failed_action": "ignore",
    "enable": true
  }'
```

`<floodgate address>` is wherever EMQX can reach floodgate: the container name on a shared Docker network (`floodgate`), the Service DNS name in Kubernetes (`floodgate.<namespace>.svc` with the chart), or a host IP. Keep `auto_reconnect` short; it bounds how long protection stays off after floodgate restarts. `failed_action` makes no practical difference for `message.publish` — see [ExHook failure policy](#exhook-failure-policy).

Verify, in the dashboard under **Management → ExHook** or with:

```bash
curl -s "$EMQX/api/v5/exhooks/floodgate" -H "Authorization: Bearer $TOKEN" \
  | jq '{enable, failed: .metrics.failed, nodes: [.node_status[].status]}'
```

Expect `enable: true` and every node `connected`. EMQX 6 reports status per node under `node_status`; there is no top-level `status` field, so `jq .status` prints `null` even when the hook is working. floodgate logs `ExHook connected: EMQX <version>` when EMQX loads it at registration (or when EMQX itself restarts). It does **not** log it again when only floodgate restarts — see [Verifying after a restart or upgrade](#verifying-after-a-restart-or-upgrade).

### Docker Compose (floodgate + EMQX together)

```bash
git clone https://github.com/eric-becker/floodgate
cd floodgate
docker compose up --build -d
```

After startup, [register the ExHook](#registering-the-exhook) with the URL `http://floodgate:9000`. This stack is for local evaluation only: it publishes MQTT (1883), the EMQX dashboard (18083) and floodgate's health port on all interfaces.

### Kubernetes (Helm)

```bash
helm install floodgate ./charts/floodgate -n floodgate --create-namespace
```

See [charts/floodgate/](charts/floodgate/) for the full values reference. The chart renders a Deployment with rolling updates, a ClusterIP Service, ConfigMap, ServiceAccount, and an optional PodDisruptionBudget; ConfigMap changes trigger automatic rolling restarts via a checksum annotation. Then [register the ExHook](#registering-the-exhook) at `http://floodgate.floodgate.svc:9000`.

The flat manifests in [k8s/](k8s/) are still present for `kubectl apply -f` users but are deprecated in favor of the chart.

### Source install

**Prerequisites:** Python 3.11+ and `git`.

```bash
pip install -e ".[dev]"            # first: provides grpc_tools for the next step
./scripts/download_protobufs.sh    # Meshtastic protobufs at the pinned commit (Apache-2.0, not bundled)
./scripts/generate_protos.sh       # Python stubs into generated/

PYTHONPATH=.:generated floodgate --config config.yaml
PYTHONPATH=.:generated floodgate --config config.yaml -v   # very verbose DEBUG logging
```

The generated stubs are not part of the installed package, so `PYTHONPATH` must point at the repository root and `generated/`; without it floodgate exits with `ModuleNotFoundError: No module named 'generated'`.

## Configuration

See [config.yaml](config.yaml) for a fully annotated example.

| Key | Default | Description |
|-----|---------|-------------|
| `zerohop_enabled` | `true` | Master switch for the zero-hop modifier. |
| `zerohop_channels` | 8 standard presets | Channels whose packets get `hop_limit` and `hop_start` zeroed. |
| `drop_enabled` | `false` | Master switch for the drop filter (deny entirely). |
| `drop_channels` | `"zerohop_channels"` | Channels on which the drop filter runs. List of channels, the literal string `"zerohop_channels"` to inherit, or `null` for all channels. |
| `drop_portnums` | `[]` | Meshtastic portnums (proto enum names like `RANGE_TEST_APP`) to drop. |
| `topic_filter` | `msh/#` | MQTT topic filter floodgate asks EMQX to call it for. |
| `grpc_port` | `9000` | gRPC listen port. |
| `grpc_max_workers` | `16` | Size of the gRPC server thread pool. Match or exceed the broker's ExHook `pool_size` — see [Sizing the thread pool](#sizing-the-thread-pool). |
| `health_port` | `8080` | HTTP health check port. `GET /health` returns `{"status":"ok","stats":{...}}`. The image's Docker `HEALTHCHECK` probes 8080, so change both together. |
| `stats_interval_s` | `60` | Stats log interval in seconds. |
| `stats_log` | `true` | Log periodic stats summaries. Set `false` to disable. |
| `log_level` | `INFO` | `INFO` shows per-message outcomes. `DEBUG` adds verbose internals. |
| `log_format` | `text` | `text` for human-readable output, `json` for Loki/Grafana structured logging. Can also be set via `FLOODGATE_LOG_FORMAT` env var. |

> **Migrating from a pre-rename config?** The keys `channel_policy`, `channel_blacklist`, and `channel_whitelist` were removed. floodgate now refuses to start if they appear in `config.yaml` and prints the new equivalents. Update your config and restart.
>
> **Unknown keys are rejected too** (since 0.5.0). A misspelled key such as `zerohop_channel:` used to merge in silently and leave the default in effect; floodgate now refuses to start and lists the keys it does recognize.

**Every value is validated at startup** (since 0.6.0), and all problems are reported together:

- Booleans must be unquoted YAML booleans. A quoted `"false"` is a truthy string, so `drop_enabled: "false"` used to turn drop **on**.
- Ports must be integers from 1 to 65535; `grpc_max_workers` and `stats_interval_s` must be integers of at least 1.
- `log_level` must be a standard level name and `log_format` must be `text` or `json` (either is case-insensitive, including `FLOODGATE_LOG_FORMAT`).
- `drop_portnums` entries must be exact Meshtastic `PortNum` names; a typo such as `RANGETEST_APP` is rejected with a suggestion.
- A config file named by `--config` or `FLOODGATE_CONFIG` must exist. Running on built-in defaults because of a wrong mount path would silently turn drop off. With neither set, the defaults are used.

A configuration error exits with status 2 and a single `Configuration error:` line, no traceback.

### Zero-hop

The default `zerohop_channels` list contains the eight standard Meshtastic public channel presets:
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

The drop filter denies a publish entirely — EMQX does not deliver it to subscribers, so it doesn't appear in MQTT consumers, dashboards, or chat logs. Drop runs *before* zero-hop; a dropped packet never reaches the zero-hop check.

The motivating use case is portnum traffic that floods the standard public channels and isn't useful in MQTT downstream — for example, `RANGE_TEST_APP`. Firmware 2.8 compiles the range test module out of every build ([meshtastic/firmware#11150](https://github.com/meshtastic/firmware/pull/11150); the source file remains, the global `MESHTASTIC_EXCLUDE_RANGETEST=1` excludes it), so these come only from older nodes; firmware itself suppresses range-test uplink only when a gateway points at the default public broker, so on a private broker floodgate is the filter.
```yaml
drop_enabled: true
drop_channels: "zerohop_channels"   # same scope as zero-hop
drop_portnums:
  - "RANGE_TEST_APP"
```

`drop_channels` accepts:

- the literal string `"zerohop_channels"` to reuse the same list as `zerohop_channels` (default — keeps drop scope aligned with zero-hop scope without duplication),
- an explicit list of channel names, e.g. `["LongFast", "MediumFast"]`,
- `null` (or an empty list) to apply on all channels.

> **Encryption constraint.** `drop_portnums` only acts on packets whose portnum floodgate can read. For protobuf (`/e/`) topics that means channels using the default Meshtastic encryption key (`AQ==`). floodgate decrypts every `/e/` packet in scope with that key; on a custom-keyed channel the result is garbage, which almost always fails to parse and is delivered. Rarely, garbage can parse as a valid `Data` message with a matching portnum and be dropped by mistake — so keep `drop_channels` scoped to default-key channels rather than `null`.

> **JSON topics.** Firmware labels only some packet types in its JSON output. Range test (and most other portnums) is published with `"type": ""` — checked in firmware 2.6.11 and 2.7.11 — so floodgate cannot identify it and delivers it. Firmware 2.8 stopped publishing JSON altogether. Gateways downlink only the protobuf (`/e/`) topic, so this never reaches RF; it only affects JSON consumers.

> **Publisher behavior.** When floodgate denies a publish, EMQX still sends a normal PUBACK to the publisher — there's no protocol-level signal back to the gateway that its message was filtered. This is a property of the EMQX ExHook deny mechanism, not floodgate. Operators monitoring publisher-side success counters won't see drops; the floodgate `dropped` counter and per-message log lines are the source of truth.

## Operations

### ExHook failure policy

EMQX's `failed_action` is documented as controlling what happens to MQTT messages when floodgate
is unreachable (crash, restart, upgrade). **For `message.publish` it does not do what its name
suggests — floodgate fails _open_ either way.**

| `failed_action` | floodgate down | Observed |
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

**Monitor `metrics.failed`**, which is the only place the outage is visible (`$EMQX` and `$TOKEN` as in [Registering the ExHook](#registering-the-exhook)):

```bash
curl -s "$EMQX/api/v5/exhooks/floodgate" -H "Authorization: Bearer $TOKEN" \
  | jq '{failed: .metrics.failed, nodes: [.node_status[].status]}'
```

Alert on it increasing, or on EMQX's `exhook_call_exception` error log. Publisher-side counters
and subscriber-side delivery both look completely normal during an outage.

**A config error is an outage too.** floodgate refuses to start on an invalid config, and a
floodgate that won't start is exactly the fail-open case above — protection off, broker healthy.
The Helm chart contains this only partly: the config checksum rolls a new pod, `maxUnavailable: 0`
keeps the old one serving until the new one is Ready, and a crash-looping replacement never
becomes Ready. But the ConfigMap is updated in place, so the old pod is now holding the bad file
too and fails the next time it restarts for any reason. Use `helm upgrade --wait` (plain
`helm upgrade` reports success while the new pod crash-loops) and fix forward promptly.
**Docker Compose has no such guard**, and the static `k8s/` manifests are worse:
nothing rolls the pod on a ConfigMap edit, so a bad config sits unnoticed until the next unrelated
restart takes floodgate down. In Compose there is a second trap: if EMQX `depends_on` floodgate
being healthy (as recommended under [Startup ordering](#startup-ordering-and-upgrades)) and EMQX is
created or recreated in the same `docker compose up`, EMQX never starts and the whole broker is
down. Restart floodgate on its own first, and [verify EMQX is calling it](#verifying-after-a-restart-or-upgrade)
before walking away.

CI regression-tests the stopped-server path (`hook-down-fails-open`, `recovery-after-hook-down`),
so if a future EMQX release starts failing closed there, CI will say so. The hung-server path was
verified by hand on EMQX 6.2.0 and is not in CI.

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

### Startup ordering and upgrades

In Docker Compose, EMQX should `depends_on` floodgate (not the other way around). floodgate is a gRPC server — it starts first, listens on port 9000, and waits. EMQX connects to it after boot. See [docker-compose.yaml](docker-compose.yaml) for the reference configuration, and the config-error trap above.

In Kubernetes the chart's rolling update (`maxUnavailable: 0, maxSurge: 1`) starts the new pod and waits for it to be Ready before terminating the old one, and EMQX reconnects through the Service. Readiness means floodgate's HTTP server is up, not that EMQX has connected: `/health` reports `ok` either way.

**Measured protection gap on a restart** (Docker Compose, EMQX 6.2.0, `docker compose up -d --force-recreate floodgate` while publishing 10 packets/s): 5 of 896 packets were delivered unmodified, all within 0.4 s, and none were lost. EMQX's gRPC channel reconnects to the new container within a fraction of a second — it does not wait for `auto_reconnect` — so an upgrade costs well under a second of protection. The Kubernetes rolling update has not been measured the same way.

### Verifying after a restart or upgrade

When only floodgate restarts, EMQX's gRPC channel reconnects transparently and EMQX does **not** call `OnProviderLoaded` again. So floodgate does not log `ExHook connected` again, even though it is working. Verify instead with either:

- EMQX's hook counters: `metrics.succeed` rising and `metrics.failed` flat, and every node `connected` (the command under [Registering the ExHook](#registering-the-exhook)), or
- floodgate's own per-message log lines (`[ZEROHOP]`, `[PASSTHRU]` …) or `/health` counters rising.

A consequence: `topic_filter` is sent to EMQX only in `OnProviderLoaded`. Changing it and restarting floodgate leaves EMQX on the old filter until the hook is re-registered (or disabled and re-enabled) or EMQX restarts.

## Security

### Network exposure

| Port | Purpose | Exposure |
|------|---------|----------|
| `9000` (gRPC) | EMQX ExHook connection | **Internal only.** Never expose publicly. |
| `8080` (HTTP) | Health check `/health` | **Internal only.** Operational stats — restrict to your monitoring network. |

floodgate listens on all interfaces and has no bind-address option, so restrict exposure outside the process: publish ports on loopback or a private address in Docker, rely on a ClusterIP Service (plus a NetworkPolicy if you want one) in Kubernetes, or use a host firewall — remembering that Docker-published ports bypass ufw.

The gRPC connection between EMQX and floodgate is **unencrypted** (no TLS). This is acceptable when both services run in the same Kubernetes namespace or Docker network on a trusted private network. Do not route this port through a public-facing load balancer or ingress.

The Helm chart and the manifests in [k8s/](k8s/) use `type: ClusterIP`, so neither port is externally reachable by default.

### Container hardening

The container image runs as `nobody` (UID 65534) and needs no writable paths. The Helm chart additionally sets a read-only root filesystem, drops all Linux capabilities and disables privilege escalation; outside the chart, apply the same with `docker run --read-only --cap-drop ALL --security-opt no-new-privileges`. See [Dockerfile](Dockerfile) and the chart's [containerSecurityContext defaults](charts/floodgate/values.yaml).

## Verifying operation

### Health endpoint

floodgate exposes a health check at `GET /health` on port 8080 (configurable via `health_port`). `status: ok` means the process is serving; it does **not** mean EMQX is connected — check the ExHook's `node_status` in EMQX for that. Stats are cumulative lifetime counters that persist across stats reporter intervals:

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
2026-10-11 01:52:16 INFO     [floodgate.zerohop] [ZEROHOP] topic=msh/US/2/e/LongFast/!a2e1a8c4 channel=LongFast encoding=e id=3827461829 from=!a2e1a8c4 to=!ffffffff hop_limit=3 hop_start=3 relay=c4 via_mqtt=False
2026-10-11 01:52:16 INFO     [floodgate.zerohop] [NOOP] topic=msh/US/2/e/LongFast/!b3c4d5e6 channel=LongFast encoding=e id=2019283746 from=!b3c4d5e6 to=!ffffffff hop_limit=0 hop_start=3 relay=c4 via_mqtt=False
2026-10-11 01:52:16 INFO     [floodgate.zerohop] [DROPPED] topic=msh/US/2/e/LongFast/!c1c2c3c4 channel=LongFast encoding=e portnum=RANGE_TEST_APP id=694258842 from=!c1c2c3c4 to=!ffffffff
2026-10-11 01:52:16 INFO     [floodgate.zerohop] [PASSTHRU] topic=msh/US/2/e/MyPrivate/!a2e1a8c4 channel=MyPrivate encoding=e id=1234567890 from=!a2e1a8c4 to=!ffffffff hop_start=3 relay=c4 via_mqtt=False
2026-10-11 01:52:16 WARNING  [floodgate.zerohop] [WARN] topic=msh/US/2/json/LongFast/!a2e1a8c4 channel=LongFast encoding=json bytes=3
2026-10-11 01:53:16 INFO     [floodgate.exhook_server] [STATS] interval_s=60 zerohop=142 passthru=1 noop=0 dropped=4 skipped=1050 errors=0 total=1197
```

`hop_limit` and `hop_start` on `[ZEROHOP]` lines are the sender's original values; the delivered packet has both at 0. A value containing a space, `=`, quote, backslash or control character is double-quoted with those characters escaped, so a crafted topic cannot forge a log line or a `key=value` field.

**JSON mode** (`log_format: json` or `FLOODGATE_LOG_FORMAT=json`); timestamps are UTC:
```json
{"timestamp": "2026-10-11T01:52:16Z", "level": "INFO", "name": "floodgate.zerohop", "message": "zerohop", "event": "message", "outcome": "zerohop", "topic": "msh/US/2/e/LongFast/!a2e1a8c4", "channel": "LongFast", "encoding": "e", "id": 3827461829, "from": "!a2e1a8c4", "to": "!ffffffff", "hop_limit": 3, "hop_start": 3, "relay": "c4", "via_mqtt": false}
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
  hop_limit:  3            ← a downlinking gateway would retransmit this on RF
  hop_start:  3
  channel:    8            ← channel hash (LongFast + default key)
  payload:    <encrypted Data protobuf>
}
```

**After** (returned to EMQX, delivered to all subscribers):
```
MeshPacket {
  from:       0xa2e1a8c4
  to:         0xffffffff
  id:         3827461829
  hop_limit:  0            ← zeroed — a gateway shows it but does not retransmit it
  hop_start:  0            ← zeroed so receivers don't render ghost hops in traceroute
  channel:    8
  payload:    <encrypted Data protobuf>   (unchanged)
}
```

The JSON topic mirror (`/json/LongFast/!a2e1a8c4`) is zeroed the same way: `hop_limit` is set to `0` (added if absent, since firmware's JSON omits it), and `hop_start` and `hops_away` are zeroed when present.

## Development

```bash
pip install -e ".[dev]"
./scripts/download_protobufs.sh   # Meshtastic protobufs at the pinned commit (not bundled)
./scripts/generate_protos.sh      # Python stubs into generated/
pytest tests/ --ignore=tests/test_container_smoke.py -q   # no Docker required
pytest tests/ -q                  # adds the container smoke test (requires Docker)
./scripts/run-integration.sh      # EMQX + floodgate + a real meshtasticd node (requires Docker)
```

Without the generated stubs, the protobuf tests skip rather than fail, so a low pass count usually means `generated/` is missing (CI sets `FLOODGATE_NO_SKIPS=1` to make any skip fatal). Contributor workflow, test tiers and the release process are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Legal

This project is not affiliated with, endorsed by, or officially associated with the Meshtastic project or Meshtastic LLC. Meshtastic® is a registered trademark of Meshtastic LLC.

floodgate is independent software that interoperates with Meshtastic's MQTT packet format. Use of the Meshtastic name is solely for the purpose of identifying compatibility.

## License

GPL v3.0 or later — see [LICENSE](LICENSE). The Meshtastic protobufs and the EMQX ExHook proto are Apache-2.0; the container image includes code generated from them — see [NOTICE](NOTICE).
