# Operating floodgate

## Failure behavior

EMQX calls floodgate on every publish. If the call fails, EMQX delivers the message **unmodified**, whatever the ExHook's `failed_action` is set to. An unreachable server makes the gRPC call raise, EMQX's hook runner catches the exception and continues with the original message, and `failed_action` is never consulted. That setting only applies to a call that returns a failure.

So when floodgate is down, the broker keeps working and nothing is dropped, but zero-hop is off and gateways rebroadcast everything. floodgate's own logs are silent because it is not running.

**Monitor `metrics.failed`** on the ExHook, or EMQX's `exhook_call_exception` log line. Publisher and subscriber metrics look normal during an outage.

```bash
curl -s "$EMQX/api/v5/exhooks/floodgate" -H "Authorization: Bearer $TOKEN" \
  | jq '{failed: .metrics.failed, nodes: [.node_status[].status]}'
```

The same applies to slow calls. A call that exceeds the broker's `request_timeout` counts as failed, and that message goes out unmodified.

## Configuration changes

floodgate refuses to start on an invalid config, and a floodgate that does not start is the fail-open case above.

- **Helm:** a config change rolls a new pod, and `maxUnavailable: 0` keeps the old pod serving until the new one is Ready. The ConfigMap is updated in place, though, so the old pod also fails if it restarts later. Use `helm upgrade --wait` and fix a bad config promptly.
- **Docker Compose:** nothing guards against a bad config. If EMQX `depends_on` floodgate being healthy and both are recreated in one `docker compose up`, EMQX does not start either. Restart floodgate on its own, and check that EMQX is calling it.
- **Static manifests in `k8s/`:** editing the ConfigMap does not restart the pod, so a bad config goes unnoticed until the next restart. Prefer the chart.

## Thread pool

EMQX calls the ExHook over a connection pool (`pool_size`, default 8). floodgate serves those calls from `grpc_max_workers` threads (default 16). If floodgate's pool is the smaller one, calls queue; a call that queues past `request_timeout` fails, and that message is delivered unmodified.

Keep `grpc_max_workers` at or above the broker's `pool_size`. Extra threads are cheap. More workers do not add throughput, because per-message work is CPU-bound and serialized by Python's GIL.

## Restarts and upgrades

- **Startup order:** floodgate is the server and EMQX connects to it. In Compose, EMQX should depend on floodgate, not the reverse. See [docker-compose.yaml](../docker-compose.yaml).
- **Kubernetes:** the chart's rolling update (`maxUnavailable: 0`, `maxSurge: 1`) waits for the new pod to be Ready before stopping the old one. Ready means floodgate's HTTP server is up, not that EMQX has connected.
- **Gap on restart:** EMQX reconnects to a restarted floodgate within a fraction of a second, so a restart leaves a handful of messages unmodified.
- **Verifying:** after floodgate alone restarts, EMQX reconnects without re-running provider registration, so floodgate does not log `ExHook connected` again. Check that EMQX's `metrics.succeed` is rising and `metrics.failed` is flat, or that floodgate's per-message logs and `/health` counters are rising.
- **`topic_filter`:** EMQX reads it only at registration. After changing it, re-register the hook (or disable and re-enable it), or restart EMQX.

## Security

| Port | Purpose |
|---|---|
| 9000 | ExHook gRPC, unauthenticated and unencrypted |
| 8080 | `GET /health`, operational counters |

Keep both internal. floodgate listens on all interfaces, so restrict exposure outside the process:
- in Docker, publish ports on loopback or a private address, remembering that Docker-published ports bypass host firewalls such as ufw;
- in Kubernetes, use a ClusterIP Service, optionally with a NetworkPolicy;
- otherwise, use a host firewall.

The image runs as `nobody` (UID 65534) and needs no writable paths. The Helm chart also sets a read-only root filesystem, drops all capabilities and disables privilege escalation. Outside the chart, the equivalent is `docker run --read-only --cap-drop ALL --security-opt no-new-privileges`.

## Health and logs

`GET /health` returns `{"status": "ok", "stats": {...}}` with lifetime counters per outcome. `ok` means the process is serving; it does not mean EMQX is connected.

Per-message lines are logged at `INFO`. Hop values in them are the sender's originals, not the rewritten ones. Values containing spaces, quotes, `=` or control characters are quoted and escaped, so a crafted topic cannot forge a log field.

With `log_format: json`, the `message` field is the outcome and all data is in top-level fields; timestamps are UTC. Example LogQL:

```
{container="floodgate"} | json | outcome="zerohop"
{container="floodgate"} | json | channel="LongFast"
sum by (outcome) (count_over_time({container="floodgate"} | json | event="message" [5m]))
```

## Drop filter details

- **Encryption:** the drop filter decrypts every in-scope `/e/` packet with the default channel key to read its portnum. On a custom-keyed channel the result is almost always unparseable, and the packet is delivered. Rarely, it parses as a matching portnum and is dropped by mistake, so keep `drop_channels` limited to default-key channels.
- **JSON topics:** firmware leaves the `type` empty for most portnums, range test included, so floodgate cannot identify them and delivers them. Firmware 2.8 no longer publishes JSON, and gateways never downlink it.
- **Publishers are not told.** EMQX acknowledges a dropped publish normally. floodgate's `dropped` counter and log lines are the record.
- **Range test:** firmware 2.8 builds no longer include the range test module, so `RANGE_TEST_APP` traffic now comes only from older nodes. Firmware filters it itself only when the gateway uses the public broker, so on a private broker floodgate is the filter.
