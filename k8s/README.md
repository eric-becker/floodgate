# Static manifests (deprecated)

These flat manifests are kept for users who deploy with `kubectl apply -f k8s/` and don't want to introduce Helm. They are scheduled to be removed in a future release in favour of the [Helm chart](../charts/floodgate/), which is now the supported install path.

If you're starting fresh, use the chart instead:

```bash
helm install floodgate ./charts/floodgate -n floodgate --create-namespace
```

The chart renders a superset of what's here (Deployment, Service, ConfigMap, plus a ServiceAccount, optional PodDisruptionBudget, and config-checksum-driven rolling restarts) and exposes every option in [config.yaml](../config.yaml) via `values.yaml`.
