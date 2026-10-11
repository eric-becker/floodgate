# floodgate

> **`CLAUDE.md` is a symlink to this file — they are one file, not two.** Edit `AGENTS.md`.
> Agents that look for either name get the same content, so guidance never drifts between them.

MQTT anti-flood service for Meshtastic with two operations:

- **Zero-hop** — set `MeshPacket.hop_limit` and `hop_start` to 0 in-flight, then deliver. Default for the standard public channel presets. A downlinking gateway shows the packet but never retransmits it on RF; private channels pass through untouched so they keep bridging MQTT and RF.
- **Drop** — deny a publish entirely so EMQX never delivers it. Off by default; intended for portnum-based spam (e.g. `RANGE_TEST_APP` on the public channels, which only pre-2.8 firmware emits).

Drop runs *before* zero-hop. Both are evaluated per message via the EMQX ExHook gRPC interface.

## Architecture

```
Gateway → EMQX → [ExHook gRPC] → floodgate → drop / modify / passthru → EMQX → Subscribers
```

| File | Role |
|------|------|
| `src/floodgate/exhook_server.py` | gRPC server; EMQX connects here. Maps process_message actions to ExHook responses. |
| `src/floodgate/zerohop.py`       | Per-message pipeline: drop check → zerohop check → structured logging. |
| `src/floodgate/config.py`        | Config loader, schema validation (rejects removed and unknown keys), policy helpers `should_zerohop` / `should_drop`. |
| `src/floodgate/decrypt.py`       | AES-128-CTR decryption with the Meshtastic default key (drop's portnum lookup needs this for `/e/` packets). |
| `src/floodgate/portnum.py`       | Portnum extraction from `/e/` (decrypt + Data parse) and `/json/` payloads. |
| `src/floodgate/log_setup.py`     | Logging formatter; text and JSON (Loki) modes. |
| `src/floodgate/health.py`        | HTTP health check server on `health_port`. |
| `src/floodgate/__main__.py`      | CLI entry point. |
| `proto/emqx/exhook.proto`        | EMQX ExHook interface definition. |
| `docker-compose.test.yaml`       | Integration test stack (emqx, floodgate, exhook-init, mesh-sim, test-driver) on an isolated bridge network; host ports on loopback only. |
| `scripts/run-integration.sh`     | Integration harness orchestrator — `--keep` leaves the stack up, `--teardown` removes it. |
| `tests/integration/`             | Integration test assets: floodgate config, ExHook init container, test-driver image + cases. Four passes: `default`, `hook-down`, `recovery`, `firmware` (a real meshtasticd node, pinned by digest). |
| `tests/test_roundtrip.py`        | Round-trip fidelity: ciphertext byte-identical, packet still decrypts, only `hop_limit`/`hop_start` change, unknown fields survive. Guards the silent-corruption class field assertions can't see. |
| `charts/floodgate/`              | Helm chart (supported install path). Values mirror `config.yaml` 1:1; rendered chart is validated in CI. |
| `k8s/`                           | Static `kubectl apply -f` manifests, deprecated in favor of the chart. |
| `.github/workflows/ci.yml`       | The only workflow: version, lint, test, smoke, integration, manifests, then image push and GitHub release (push/tag only, after everything passes). |
| `scripts/check_version.sh`       | Fails unless pyproject, `__version__`, chart `appVersion`, the `k8s/` image tag (and the tag, when given) agree. |
| `scripts/check_chart.py`         | Loads a rendered chart's ConfigMap with `load_config` and checks the rollout settings that contain a bad config. |
| `NOTICE`                         | Third-party attribution (Apache-2.0 Meshtastic protobufs and EMQX ExHook proto). |

## Dev Setup

```bash
git clone https://github.com/eric-becker/floodgate
cd floodgate
pip install -e ".[dev]"           # first: grpc_tools is needed to generate stubs
./scripts/download_protobufs.sh   # Meshtastic protobufs at the pinned commit → protobufs/
./scripts/generate_protos.sh      # Python stubs → generated/
pytest tests/ --ignore=tests/test_container_smoke.py -q   # no Docker required
pytest tests/ -q   # full suite including container smoke test (requires Docker)
./scripts/run-integration.sh   # full Docker Compose end-to-end test (requires Docker)
```

Routing-logic tests mock the low-level zerohop functions, so the suite runs
without Meshtastic protobufs (handy for fast iteration). Tests that exercise
real protobuf payloads (under `tests/payloads/protobuf/`, plus the decrypt
and portnum tests that build synthetic envelopes) skip automatically via
`pytest.importorskip("meshtastic")` when the generated stubs aren't present.
CI generates the stubs and sets `FLOODGATE_NO_SKIPS=1`, so any skip fails CI.

## Running

```bash
PYTHONPATH=.:generated floodgate --config config.yaml
PYTHONPATH=.:generated floodgate --config config.yaml -v   # verbose DEBUG logging (decode steps, gRPC calls)
```

The generated stubs are not packaged, so `PYTHONPATH` must include the repo root and `generated/` (the image sets it itself).

Per-message outcomes (`[ZEROHOP]`, `[PASSTHRU]`, `[NOOP]`, `[DROPPED]`) are logged at **INFO** level. No
special flag needed to see them. `-v` enables DEBUG for internal decode and gRPC detail.

Or with Docker Compose (includes EMQX):
```bash
docker compose up --build
```

After startup, register the ExHook in EMQX (README, "Registering the ExHook").

## Branching & Release Strategy

- `main` — always releasable; releases are tags on `main`
- `feat/<name>`, `fix/<name>`, `docs/<name>`, `chore/<name>` — branch from main, PR back to main
- Commits: conventional format — `feat:`, `fix:`, `docs:`, `test:`, `chore:`
- PRs: squash-merge to main; delete branch after merge
- Releases: bump the four version strings `scripts/check_version.sh` checks, merge, then `git tag vX.Y.Z && git push origin vX.Y.Z`. `ci.yml` runs every job on the tag and only then pushes the image and creates the GitHub release
- Pinned on purpose: the protobufs commit (`PROTOBUFS_REF` in `scripts/download_protobufs.sh`), `requirements.txt`, the Dockerfile base image digest and ruff. Bump them in a PR; never unpin
- See [CONTRIBUTING.md](CONTRIBUTING.md) for full release workflow details

## Key Config Options

Drop runs **before** zerohop. A packet matched by drop is denied entirely and never reaches the zerohop check.

| Key | Default | Effect |
|-----|---------|--------|
| `zerohop_enabled` | `true` | Master switch for the zero-hop modifier. |
| `zerohop_channels` | 8 standard presets | Channels whose packets get `hop_limit` and `hop_start` zeroed. |
| `drop_enabled` | `false` | Master switch for the drop filter. |
| `drop_channels` | `"zerohop_channels"` | Drop scope. List of channels, the literal string `"zerohop_channels"` to inherit, or `null` for all channels. |
| `drop_portnums` | `[]` | Exact Meshtastic `PortNum` names (like `RANGE_TEST_APP`) to drop; unknown names are rejected. Readable only on default-key (`AQ==`) protobuf channels and for the few JSON types firmware labels (range test is not one). A custom-keyed packet can, rarely, decrypt into a matching portnum, so keep `drop_channels` scoped. |
| `grpc_max_workers` | `16` | gRPC server thread-pool size. Must be >= the broker's ExHook `pool_size` or calls queue; a queued call past `request_timeout` is counted failed, and a failed call is **delivered unmodified** — so an undersized pool silently loses zero-hop protection rather than dropping packets. See README "ExHook failure policy". |
| `log_level` | `INFO` | `INFO` logs per-message outcomes. `DEBUG` adds decode/gRPC internals. |
| `log_format` | `text` | `text` (default) or `json` for Loki/Grafana; JSON timestamps are UTC. Override with `FLOODGATE_LOG_FORMAT` env var. |
| `stats_log` | `true` | Log periodic stats summaries. Set `false` to disable. |

The pre-rename keys `channel_policy` / `channel_blacklist` / `channel_whitelist` were removed. floodgate refuses to load a config containing any of them and prints the new equivalents.
Any key not in `DEFAULT_CONFIG` (`src/floodgate/config.py`) is also rejected at startup, so a typo (`zerohop_channel:`) fails loudly instead of silently leaving the default in effect. Every value is type- and range-checked too (unquoted booleans, ports 1-65535, known log levels/formats, real `PortNum` names, an existing config file when one is named); `load_config` collects all value errors into one `ConfigError`, and the CLI exits 2 on it.

## Documentation and Test Discipline

When making code changes, **always update in the same PR**:

- `README.md` — any changed behavior, config options, log output examples, or CLI flags
- `AGENTS.md` — file table, config table, and running instructions (`CLAUDE.md` is a symlink to it)
- `config.yaml` — if new config keys are added
- `tests/` — add or update tests for changed behavior before the PR is created
- `CONTRIBUTING.md` — if CI jobs, test tiers, or branch strategy changes

Do not open a PR until README and tests reflect the code changes being merged.

## Local-only paths (never commit)

These paths exist in working trees but are gitignored. Never stage them:

- `.claude/` — local Claude Code settings
- `docs/superpowers/` — planning artifacts written by Claude's superpowers skills
- `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `coverage*` — Python build/test artefacts
- `generated/` — Python protobuf stubs (built by `scripts/generate_protos.sh`)
- `protobufs/meshtastic/`, `protobufs/nanopb.proto`, `protobufs/LICENSE`, `protobufs/.ref` — upstream Meshtastic protobufs at the pinned commit (written by `scripts/download_protobufs.sh`); only `protobufs/README.md` is tracked

Before starting work on a branch, `git fetch origin && git rebase origin/main` to pick up any landed changes to `.gitignore` or shared workflows.
