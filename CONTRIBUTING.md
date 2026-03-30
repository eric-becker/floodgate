# Contributing

## Reporting issues

Use GitHub Issues. Bug reports should include your EMQX version, floodgate version, deployment method (Docker/k8s/source), relevant `config.yaml` snippet, and log output at DEBUG level.

## Submitting changes

1. Fork the repo and create a branch: `feat/<name>`, `fix/<name>`, or `docs/<name>`
2. Make your changes with tests where applicable
3. Confirm `pytest tests/ -q` passes locally — no protobufs needed, imports are mocked
4. Open a PR against `main`; CI must be green before merge

All PRs are squash-merged. One PR per feature or fix.

## Commit style

Conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `chore:`

## Releases

Maintainers tag releases on `main` as `vMAJOR.MINOR.PATCH`. The release workflow runs the full test matrix and creates a GitHub release automatically.

## Protobuf compatibility

floodgate targets compatibility with the Meshtastic protobuf schema as published at
[meshtastic/protobufs](https://github.com/meshtastic/protobufs). If a schema change breaks
something, open an issue with the affected `ServiceEnvelope` fields and the version where the
regression was introduced.
