# Contributing

## Reporting issues

Use GitHub Issues. Bug reports should include your EMQX version, floodgate version, deployment method (Docker/k8s/source), relevant `config.yaml` snippet, and log output at DEBUG level.

## Submitting changes

1. Fork the repo and create a branch: `feat/<name>`, `fix/<name>`, or `docs/<name>`
2. Make your changes with tests where applicable
3. Confirm `pytest tests/ --ignore=tests/test_container_smoke.py -q` passes locally
4. Open a PR against `main`; all CI jobs must be green before merge

All PRs are squash-merged. One PR per feature or fix.

## CI jobs

Every PR and push to `main` runs four jobs in sequence:

| Job | What it checks |
|-----|----------------|
| **lint** | `ruff` style and import checks |
| **unit tests** | Pure Python tests across Python 3.11/3.12/3.13 — no external services needed. Protobuf imports are mocked so no protobufs required. |
| **container smoke** | Builds the Docker image, starts the container, and verifies `/health` returns `200 OK`. Catches Dockerfile bugs and runtime import errors that unit tests cannot. |
| **manifest validation** | Validates `k8s/*.yaml` against the Kubernetes schema with `kubeconform`. |

### Running locally

```bash
# Unit tests (fast, no Docker needed)
pytest tests/ --ignore=tests/test_container_smoke.py -q

# With coverage
pytest tests/ --ignore=tests/test_container_smoke.py --cov=src/floodgate --cov-report=term-missing

# Container smoke tests (requires Docker)
pytest tests/test_container_smoke.py -m smoke -v

# Lint
ruff check src/ tests/
```

## Commit style

Conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `chore:`

## Releases

Maintainers tag releases on `main` as `vMAJOR.MINOR.PATCH`. The release workflow runs lint, the full test matrix, and the container smoke test before creating a GitHub release automatically.

## Protobuf compatibility

floodgate targets compatibility with the Meshtastic protobuf schema as published at
[meshtastic/protobufs](https://github.com/meshtastic/protobufs). If a schema change breaks
something, open an issue with the affected `ServiceEnvelope` fields and the version where the
regression was introduced.
