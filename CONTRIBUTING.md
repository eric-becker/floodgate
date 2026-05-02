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

Every PR and push to `main` runs five jobs in sequence:

| Job | What it checks |
|-----|----------------|
| **lint** | `ruff` style and import checks |
| **unit tests** | Pure Python tests across Python 3.11/3.12/3.13 — no external services needed. CI generates the Meshtastic protobuf stubs before running so the unmocked protobuf payload tests in `tests/payloads/protobuf/` are exercised. Mocked tests in the rest of the suite still run without protobufs (handy for fast local iteration). |
| **container smoke** | Builds the Docker image, starts the container, and verifies `/health` returns `200 OK`. Catches Dockerfile bugs and runtime import errors that unit tests cannot. |
| **integration** | Brings up `docker-compose.test.yaml` (EMQX + floodgate + test-driver) and runs `drop` / `zerohop` / `passthru` / `noop` / `custom-key passthru` end-to-end. See "Integration testing" below. |
| **manifest validation** | Validates `k8s/*.yaml` against the Kubernetes schema with `kubeconform`. |

### Running locally

```bash
# Optional: keep __pycache__ out of the working tree.
export PYTHONDONTWRITEBYTECODE=1

# Unit tests (fast, no Docker needed). Protobuf-dependent tests skip
# automatically if you haven't generated stubs yet.
pytest tests/ --ignore=tests/test_container_smoke.py -q

# Generate protobuf stubs once for full coverage of /e/ payload tests
./scripts/download_protobufs.sh
./scripts/generate_protos.sh

# With coverage
pytest tests/ --ignore=tests/test_container_smoke.py --cov=src/floodgate --cov-report=term-missing

# Container smoke tests (requires Docker)
pytest tests/test_container_smoke.py -m smoke -v

# Lint
ruff check src/ tests/
```

### Integration testing

The integration harness (`scripts/run-integration.sh`) brings up a full Docker Compose stack — EMQX + floodgate + an ExHook auto-registration container + a Python test-driver — on an isolated bridge network and runs end-to-end checks for `drop`, `zerohop`, `passthru`, `noop`, and `custom-key channel passthru`. The test-driver crafts real Meshtastic `ServiceEnvelope` protobufs (using the same `meshtastic` Python library the firmware uses internally), so each case exercises the exact wire format floodgate sees in production.

Each case verifies BOTH what the subscriber received (delivered MQTT bytes) AND floodgate's `/health` stats — a behavior change with no stat increment, or a stat increment with no delivery effect, both fail the case. One PASS or FAIL line is printed per case.

Requirements: `docker` and `bash`. The `pytest` suite never runs the harness — it's opt-in via the script.

```bash
# One-shot verification — brings the stack up, runs cases, tears down, exits 0/non-zero.
./scripts/run-integration.sh

# Ad-hoc poking — leave the stack running after the cases finish.
./scripts/run-integration.sh --keep
#   floodgate /health: http://localhost:18089/health
#   EMQX dashboard:    http://localhost:18083  (admin / public)

# Tear the stack down (and volumes/network) without running cases.
./scripts/run-integration.sh --teardown
```

CI runs the same script in the `integration` job after the `smoke` job passes. A failed case dumps service logs into the workflow output before tearing down.

## Commit style

Conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `chore:`

## Branching strategy

- `main` — stable, tagged releases only
- Feature branches: `feat/<name>`, `fix/<name>`, `docs/<name>`, `chore/<name>`
- Branch from `main`, PR back to `main`, squash-merge
- Delete feature branches after merge (GitHub auto-deletes remote; clean local with `git branch -d`)

## Release workflow

Releases use semantic versioning (`v0.2.1`, `v1.0.0`, etc.).

### Steps

1. Merge PR(s) to `main`
2. Tag the release:
   ```bash
   git checkout main && git pull
   git tag v0.X.Y
   git push origin v0.X.Y
   ```
3. Two workflows trigger automatically on tag push:
   - **Release** (`release.yml`): lint → test → smoke → creates a GitHub Release with auto-generated notes
   - **Docker** (`docker-publish.yml`): builds multi-arch image (amd64 + arm64) and pushes to `ghcr.io/eric-becker/floodgate:<version>`
4. Update downstream deployments to reference the new image tag

### What gets published

| Trigger | Image tags pushed to GHCR |
|---------|---------------------------|
| Push to `main` | `latest` |
| Tag `v0.2.1` | `0.2.1`, `0.2`, `latest` |
| PR (build-only) | `pr-<number>` (not pushed) |

### Version bumping

- Patch (`v0.2.0` → `v0.2.1`): bug fixes, logging changes, config additions
- Minor (`v0.2.1` → `v0.3.0`): new features, behavior changes
- Major (`v0.3.0` → `v1.0.0`): breaking config or API changes

The version in `pyproject.toml` is the Python package version and does not need to match the Docker image tag — the git tag is the source of truth for releases.

## Protobuf compatibility

floodgate targets compatibility with the Meshtastic protobuf schema as published at
[meshtastic/protobufs](https://github.com/meshtastic/protobufs). If a schema change breaks
something, open an issue with the affected `ServiceEnvelope` fields and the version where the
regression was introduced.
