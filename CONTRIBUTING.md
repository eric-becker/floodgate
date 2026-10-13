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

Every pull request, push to `main` and release tag runs one workflow, `.github/workflows/ci.yml`. Nothing is published unless every test job passes.

| Job | What it checks |
|-----|----------------|
| **version** | `scripts/check_version.sh`: `pyproject.toml`, `__version__`, the chart's `appVersion` and the `k8s/deployment.yaml` image tag agree, and on a tag push they match the tag. |
| **lint** | `ruff check` and `ruff format --check`, with the ruff version pinned in `pyproject.toml`. |
| **test** | Unit tests on Python 3.11, 3.12 and 3.13, with the Meshtastic stubs generated at the pinned protobufs commit. `FLOODGATE_NO_SKIPS=1` makes any skipped test fail the run, so missing stubs can't silently disable the protobuf tests. |
| **smoke** | Builds the image, starts it, and checks `/health`. Catches Dockerfile bugs and runtime import errors. |
| **integration** | `scripts/run-integration.sh`: EMQX, floodgate and a real `meshtasticd` node in Docker Compose. See "Integration testing" below. |
| **manifests** | `kubeconform` on `k8s/*.yaml` and two rendered chart variants, `helm lint`, and `scripts/check_chart.py`, which loads the rendered ConfigMap with floodgate's own config loader and checks the rollout settings that contain a bad config. |
| **image** | Multi-arch (amd64, arm64) build after all of the above. Pushed only for `main` and release tags; pull requests build without pushing. |
| **release** | On a release tag, after the image is pushed: a GitHub release with generated notes. |

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

# Lint and formatting (CI fails on either)
ruff check .
ruff format --check .      # `ruff format .` to fix
```

### Integration testing

The integration harness (`scripts/run-integration.sh`) brings up a full Docker Compose stack — EMQX, floodgate, an ExHook registration container, a real `meshtasticd` node (`mesh-sim`) and a Python test-driver — on an isolated bridge network, and runs four passes: `default` (zerohop, zerohop-multihop, drop, passthru, noop, custom-key-not-dropped, json-zerohop), `hook-down` (floodgate stopped: fails open), `recovery`, and `firmware` (the real node accepts a zero-hopped packet; traceroutes, direct and through recorded relays, are answered with the ghost-hop count the hop rule implies). The test-driver builds `ServiceEnvelope`s with the `meshtastic` Python package's generated protobufs — the same schema as firmware — so each case exercises the wire format floodgate sees in production. See [tests/integration/README.md](tests/integration/README.md) for what each pass proves.

The `default` cases check both what the subscriber received and floodgate's `/health` deltas; `hook-down` checks EMQX's ExHook `metrics.failed`, `recovery` checks delivered hop fields, and `firmware` checks what the node accepted. One PASS or FAIL line is printed per case.

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

- `main` — always releasable; releases are tags on `main`
- Feature branches: `feat/<name>`, `fix/<name>`, `docs/<name>`, `chore/<name>`
- Branch from `main`, PR back to `main`, squash-merge
- Delete feature branches after merge (GitHub auto-deletes remote; clean local with `git branch -d`)

## Release workflow

Releases use semantic versioning. Before 1.0, a **minor** release may change the config schema (0.3.0 renamed keys; 0.5.0 and 0.6.0 tightened validation), so read the release notes before upgrading.

### Steps

1. In a PR, bump the version in all four places `scripts/check_version.sh` checks: `pyproject.toml`, `src/floodgate/__init__.py`, `charts/floodgate/Chart.yaml` (`appVersion`) and the image tag in `k8s/deployment.yaml`. If anything under `charts/` changed, also bump the chart's own `version`. Merge it.
2. Tag the merge commit:
   ```bash
   git checkout main && git pull
   git tag v0.X.Y
   git push origin v0.X.Y
   ```
3. CI runs every job on the tag. If they all pass, it pushes the image and creates the GitHub release. A failing job publishes nothing; fix forward with a new patch version rather than moving the tag.
4. Update downstream deployments to the new image tag.

### What gets published

| Trigger | Image tags pushed to GHCR |
|---------|---------------------------|
| Push to `main` | `latest` |
| Tag `v0.6.1` | `0.6.1`, `0.6` |
| Pull request | nothing (built only) |

`latest` and a release tag of the same commit come from separate builds, so their digests differ. Deployments should pin a release tag.

### Dependencies

`requirements.txt` (the image's runtime dependencies) and `requirements-build.txt` (`grpcio-tools`, builder stage only) pin every package, transitive ones included, with hashes; the image installs them with `--require-hashes`. They are compiled from `requirements.in` / `requirements-build.in` with the `pip-compile` commands in those files, under Python 3.13 (the image's). To change a dependency, edit the `.in` file and recompile; never hand-edit the `.txt`. CI installs the same hashed set before floodgate's dev tools, so the tests run against what the image ships. The Dockerfile's base image is pinned by digest. Dependabot proposes monthly bumps for pip, Docker and GitHub Actions, and CI decides. `pyproject.toml` keeps looser ranges for source installs, and pins ruff, which CI reads from there.

## Protobuf compatibility

floodgate compiles the Meshtastic protobuf schema from
[meshtastic/protobufs](https://github.com/meshtastic/protobufs) at the commit pinned in
`scripts/download_protobufs.sh` (`PROTOBUFS_REF`), so every build uses the same schema. To move to a
newer schema, bump that commit in a PR and let the full suite (including the firmware integration
tier) run. If a schema change breaks something, open an issue with the affected `ServiceEnvelope`
fields and the commit where the regression was introduced.
