# floodgate

Helm chart for [floodgate](https://github.com/eric-becker/floodgate), an MQTT anti-flood service for Meshtastic. floodgate plugs into EMQX via the ExHook gRPC interface and either zero-hops in-flight `MeshPacket`s on public channels (`hop_limit` set to 0) or drops them outright by portnum.

## Install

```bash
# Default: zero-hop the standard public channels, drop disabled, text logs
helm install floodgate ./charts/floodgate -n floodgate --create-namespace

# Common: enable RANGE_TEST_APP drop, JSON logs for Loki
helm install floodgate ./charts/floodgate -n floodgate --create-namespace \
  --set config.drop_enabled=true \
  --set 'config.drop_portnums={RANGE_TEST_APP}' \
  --set config.log_format=json
```

After install, register the ExHook on EMQX with the URL `http://floodgate.floodgate.svc:9000` (release `floodgate` in namespace `floodgate`; `helm install` prints yours). The steps — log in for a token, then `POST /api/v5/exhooks` — are in the project README under [Registering the ExHook](../../README.md#registering-the-exhook). EMQX's `/api/v5` does not accept dashboard credentials as basic auth, so `curl -u admin:<password>` returns 401.

## Upgrade

```bash
helm upgrade floodgate ./charts/floodgate -n floodgate -f my-values.yaml
```

ConfigMap changes trigger a rolling restart automatically — the Deployment carries a `checksum/config` annotation that the chart recomputes from the rendered ConfigMap on every render. floodgate validates its config at startup and refuses to start on any invalid value, so use `--wait`: without it `helm upgrade` reports success while a new pod crash-loops. The old pod keeps serving until then, but it already holds the updated ConfigMap and will fail on its next restart, so fix forward promptly.

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
| `replicaCount` | `1` | Replicas share one ExHook URL; kube-proxy spreads EMQX's `pool_size` long-lived gRPC connections across pods at connect time, not per call |
| `config.zerohop_enabled` | `true` | Master switch for the zero-hop modifier |
| `config.zerohop_channels` | 8 standard presets | Channels whose packets get `hop_limit` set to 0 |
| `config.drop_enabled` | `false` | Master switch for the drop filter |
| `config.drop_channels` | `"zerohop_channels"` | Inherits `zerohop_channels`, or a list. For **all channels** set `drop_channels: []` in a values file: Helm deletes a key set to `null` (floodgate then keeps the default scope), and `--set config.drop_channels={}` renders `[""]`, which matches nothing |
| `config.drop_portnums` | `[]` | Exact `PortNum` names, e.g. `[RANGE_TEST_APP]`; unknown names are rejected at startup |
| `config.grpc_max_workers` | `16` | gRPC thread-pool size. Match or exceed the broker's ExHook `pool_size`, or calls queue; a call past `request_timeout` counts as failed and is then **delivered unmodified**, silently losing zero-hop protection (not dropped — see README "ExHook failure policy") |
| `config.log_format` | `"text"` | Set `"json"` for Loki/Grafana |
| `service.type` | `ClusterIP` | gRPC port only; health is internal |
| `podDisruptionBudget.enabled` | `false` | Set `true` only with `replicaCount >= 2`; with one replica, `minAvailable: 1` blocks node drains |
| `resources.requests` / `limits` | 50m/200m CPU, 64Mi/128Mi memory | Tuned for ~1 broker; raise for high-throughput EMQX clusters |
| `containerSecurityContext` | nonroot, read-only fs, drop ALL caps, `RuntimeDefault` seccomp | Stricter than the image alone (which only sets `USER nobody`); meets Pod Security "restricted" |

The `config.*` block is rendered verbatim into the ConfigMap and read by floodgate via `FLOODGATE_CONFIG=/etc/floodgate/config.yaml`. Any key floodgate accepts can be set there, and nothing else: unknown keys and invalid values stop floodgate starting. See [config.yaml](../../config.yaml) and the project [README](../../README.md#configuration) for the full schema. CI renders the chart and loads the result with floodgate's own config loader.

## Health probes

The container exposes `/health` on `config.health_port` (default `8080`). The Service does **not** expose this port — health is for cluster-internal probes only. To inspect manually, port-forward the Pod:

```bash
POD=$(kubectl get pod -n floodgate -l app.kubernetes.io/name=floodgate -o name | head -1)
kubectl port-forward -n floodgate $POD 8080:8080
curl http://localhost:8080/health
```
