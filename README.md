# floodgate

Zero-hop enforcement for self-hosted [EMQX](https://www.emqx.io/) brokers serving [Meshtastic](https://meshtastic.org/) networks.

floodgate plugs into EMQX through [ExHook](https://www.emqx.io/docs/en/latest/extensions/exhook.html) (gRPC) and rewrites Meshtastic packets in flight, before any subscriber sees them. Packets on public channels get `hop_limit` 0, so gateways that downlink them show them to their own users but never rebroadcast them on LoRa. It can also drop packets outright by portnum.

Requires EMQX 5.9 or later (ExHook v3). Images for amd64 and arm64 are published to `ghcr.io/eric-becker/floodgate`.

## Why

The public broker, `mqtt.meshtastic.org`, enforces a [zero-hop policy](https://meshtastic.org/docs/software/integrations/mqtt/#zero-hop-policy) so that internet-wide MQTT traffic cannot flood local radio meshes. A private broker has no such policy, and the Meshtastic docs [warn against](https://meshtastic.org/docs/software/integrations/mqtt/#using-private-brokers) using default keys on one for that reason.

floodgate gives a private broker the same protection, with no changes to clients or gateways. It is selective: public channels are zero-hopped, while private and regional channels keep bridging between MQTT and RF.

## How it works

```
Gateway → EMQX → ExHook (gRPC) → floodgate → EMQX → subscribers and downlinking gateways
```

For each packet on a channel in `zerohop_channels`, floodgate sets `hop_limit` to 0. The `zerohop_hop_start` setting decides what happens to `hop_start`:

| Mode | Heard directly (3/3) | After two relays (1/3) | Trade-off |
|---|---|---|---|
| `keep` (default) | 0/3 | 0/3 | Identical to `mqtt.meshtastic.org`. Apps and tools show every zero-hopped packet as `hop_start` hops away, and traceroutes are padded with unknown hops. |
| `travelled` | 0/0 | 0/2 | `hop_start` becomes the hops already travelled, so hop counts and traceroutes stay accurate. The Android app shows a packet heard directly as "?" hops. |

Values are `hop_limit`/`hop_start`. Receivers compute hops travelled as `hop_start - hop_limit`.

Every message gets one outcome:

| Outcome | Meaning |
|---|---|
| `zerohop` | On a zero-hop channel: rewritten and delivered |
| `noop` | On a zero-hop channel, already `hop_limit` 0: delivered unchanged |
| `passthru` | Not on a zero-hop channel: delivered unchanged |
| `dropped` | Matched the drop filter: not delivered |
| `warn` | Could not be parsed: delivered unchanged and logged |
| `skipped` | Not a packet topic (map reports, stats): ignored |

floodgate never drops a packet because of its own error; it delivers the message unchanged and logs it.

## Quick start

### Docker

```bash
curl -fsSLo config.yaml https://raw.githubusercontent.com/eric-becker/floodgate/v0.8.0/config.yaml
docker run -d --name floodgate --restart unless-stopped \
  -v "$(pwd)/config.yaml:/app/config.yaml:ro" \
  -p 127.0.0.1:9000:9000 \
  ghcr.io/eric-becker/floodgate:0.8.0
```

Pin a version, and take `config.yaml` from the same tag: floodgate refuses to start on a key it does not recognize. Port 9000 is unauthenticated gRPC, so never publish it on a public address. If EMQX runs in Docker, put both containers on one network and omit `-p`.

### Docker Compose

`docker compose up --build -d` in a clone of this repository starts floodgate and EMQX together; then register the ExHook with the URL `http://floodgate:9000`. It is meant for evaluation: it publishes MQTT and the EMQX dashboard on all interfaces.

### Kubernetes

```bash
helm install floodgate ./charts/floodgate -n floodgate --create-namespace
```

See [charts/floodgate](charts/floodgate/) for the values reference.

### Register the ExHook

floodgate does nothing until EMQX calls it. Register it once per broker, in the dashboard under **Management → ExHook** or through the API:

```bash
EMQX=http://localhost:18083
TOKEN=$(curl -s -X POST "$EMQX/api/v5/login" -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<dashboard password>"}' | jq -r .token)

curl -s -X POST "$EMQX/api/v5/exhooks" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"floodgate","url":"http://<floodgate host>:9000","auto_reconnect":"5s","enable":true}'
```

The URL is whatever EMQX can reach: the container name on a shared Docker network, `floodgate.<namespace>.svc` in Kubernetes, or a host address. Registration worked when every node reports `connected`:

```bash
curl -s "$EMQX/api/v5/exhooks/floodgate" -H "Authorization: Bearer $TOKEN" | jq '[.node_status[].status]'
```

## Configuration

[config.yaml](config.yaml) is a fully commented example.

| Key | Default | Description |
|---|---|---|
| `zerohop_enabled` | `true` | Turns zero-hop on or off. |
| `zerohop_channels` | the 8 standard presets | Channels to zero-hop, by name. |
| `zerohop_hop_start` | `keep` | `keep` or `travelled`; see [How it works](#how-it-works). |
| `drop_enabled` | `false` | Turns the drop filter on or off. |
| `drop_channels` | `"zerohop_channels"` | Where the drop filter applies: a list of channels, `"zerohop_channels"` to reuse that list, or `null` for all. |
| `drop_portnums` | `[]` | Portnums to drop, by enum name, e.g. `RANGE_TEST_APP`. |
| `topic_filter` | `msh/#` | Topics EMQX sends to floodgate. |
| `grpc_port` | `9000` | ExHook gRPC port. |
| `grpc_max_workers` | `16` | gRPC thread pool. Keep it at or above EMQX's ExHook `pool_size`. |
| `health_port` | `8080` | Port for `GET /health`. |
| `stats_interval_s` | `60` | Interval between stats log lines. |
| `stats_log` | `true` | Log periodic stats. |
| `log_level` | `INFO` | `INFO` logs one line per message. |
| `log_format` | `text` | `text` or `json` (for Loki). Also settable with `FLOODGATE_LOG_FORMAT`. |

floodgate validates the whole file at startup. An unknown key, a wrong type or a misspelled portnum stops it with a `Configuration error:` message and exit status 2.

The drop filter reads a packet's portnum by decrypting it with the default channel key, so it only works on channels that use that key. Keep `drop_channels` scoped to them.

## Operations

- **floodgate fails open.** If it is down, EMQX delivers messages unmodified, whatever the ExHook's `failed_action` says. The broker stays up, but the mesh is unprotected. Monitor the ExHook's `metrics.failed` counter in EMQX; it is the only signal.
- **A bad config is an outage.** floodgate will not start on an invalid config, which is the fail-open case above. Validate config changes before rolling them out.
- **`/health` reports the process, not the connection.** It returns `ok` with lifetime counters even before EMQX connects. Check the ExHook's node status in EMQX for that.
- **After a floodgate restart, EMQX reconnects silently.** floodgate does not log `ExHook connected` again. Check that counters are rising instead. A changed `topic_filter` only takes effect after the hook is re-registered.
- **Keep ports 9000 and 8080 internal.** Neither is authenticated or encrypted.

More detail is in [docs/operations.md](docs/operations.md).

## Logs

Text (default):

```
INFO [floodgate.zerohop] [ZEROHOP] topic=msh/US/2/e/LongFast/!a2e1a8c4 channel=LongFast encoding=e id=3827461829 from=!a2e1a8c4 to=!ffffffff hop_limit=3 hop_start=3 relay=c4 via_mqtt=False
INFO [floodgate.exhook_server] [STATS] interval_s=60 zerohop=142 passthru=1 noop=0 dropped=4 skipped=1050 errors=0 total=1197
```

JSON (`log_format: json`) puts every field at the top level, for queries such as `{container="floodgate"} | json | outcome="zerohop"`. Hop values in log lines are the sender's originals.

## Development

```bash
pip install -e ".[dev]"
./scripts/download_protobufs.sh && ./scripts/generate_protos.sh
pytest tests/ --ignore=tests/test_container_smoke.py
./scripts/run-integration.sh    # EMQX + floodgate + a Meshtastic firmware node, in Docker
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the test suites and the release process.

## License

GPL-3.0-or-later; see [LICENSE](LICENSE). The image includes code generated from the Apache-2.0 Meshtastic and EMQX protobufs; see [NOTICE](NOTICE).

floodgate is not affiliated with or endorsed by the Meshtastic project or Meshtastic LLC. Meshtastic® is a registered trademark of Meshtastic LLC.
