# floodgate

Helm chart for [floodgate](https://github.com/eric-becker/floodgate), an MQTT anti-flood service for Meshtastic. Floodgate plugs into EMQX via the ExHook gRPC interface and either zeros the `hop_limit` of in-flight `MeshPacket`s or drops them outright by portnum.

## Install

```bash
# Default: standard public-channel zerohop, drop disabled, text logs
helm install floodgate ./charts/floodgate -n floodgate --create-namespace

# Common: enable RANGE_TEST_APP drop, JSON logs for Loki
helm install floodgate ./charts/floodgate -n floodgate --create-namespace \
  --set config.drop_enabled=true \
  --set 'config.drop_portnums={RANGE_TEST_APP}' \
  --set config.log_format=json
```

After install, register the ExHook on EMQX:

```bash
curl -u admin:public -X POST http://<emqx-host>:18083/api/v5/exhooks \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "floodgate",
    "url":  "http://floodgate.floodgate.svc:9000",
    "enable": true
  }'
```

## Upgrade

```bash
helm upgrade floodgate ./charts/floodgate -n floodgate -f my-values.yaml
```

ConfigMap changes trigger a rolling restart automatically — the Deployment carries a `checksum/config` annotation that the chart recomputes from the rendered ConfigMap on every render.

## Uninstall

```bash
helm uninstall floodgate -n floodgate
```

## Values

The full default set lives in [values.yaml](values.yaml). Highlights:

| Key | Default | Notes |
|-----|---------|-------|
| `image.repository` | `ghcr.io/eric-becker/floodgate` | Image registry/repo |
| `image.tag` | `""` (Chart `appVersion`) | Override to pin |
| `replicaCount` | `1` | Each replica registers as its own ExHook target if you front them with a Service per pod; the default cluster-internal Service round-robins |
| `config.zerohop_enabled` | `true` | Master switch for the zero-hop modifier |
| `config.zerohop_channels` | 8 standard presets | Channels whose packets get `hop_limit` zeroed |
| `config.drop_enabled` | `false` | Master switch for the drop filter |
| `config.drop_channels` | `"zerohop_channels"` | Inherits from zerohop, or a list, or `null` for all channels |
| `config.drop_portnums` | `[]` | E.g. `[RANGE_TEST_APP]` |
| `config.grpc_max_workers` | `16` | gRPC thread-pool size. Match or exceed the broker's ExHook `pool_size`, or calls queue; under `failed_action: deny` a queued call past `request_timeout` is a dropped packet |
| `config.log_format` | `"text"` | Set `"json"` for Loki/Grafana |
| `service.type` | `ClusterIP` | gRPC port only; health is internal |
| `podDisruptionBudget.enabled` | `false` | Set `true` and tune `minAvailable` for HA topologies |
| `resources.requests` / `limits` | 50m/200m CPU, 64Mi/128Mi memory | Tuned for ~1 broker; raise for high-throughput EMQX clusters |
| `containerSecurityContext` | nonroot, read-only fs, drop ALL caps | Matches the upstream Dockerfile |

The `config.*` block is rendered verbatim into the ConfigMap and read by floodgate via `FLOODGATE_CONFIG=/etc/floodgate/config.yaml`. Any key floodgate accepts can be set there; see [config.yaml](../../config.yaml) and the project [README](../../README.md) for the full schema.

## Health probes

The container exposes `/health` on `config.health_port` (default `8080`). The Service does **not** expose this port — health is for cluster-internal probes only. To inspect manually, port-forward the Pod:

```bash
POD=$(kubectl get pod -n floodgate -l app.kubernetes.io/name=floodgate -o name | head -1)
kubectl port-forward -n floodgate $POD 8080:8080
curl http://localhost:8080/health
```
