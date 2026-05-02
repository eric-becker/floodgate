# floodgate

MQTT anti-flood service for Meshtastic with two operations:

- **Zerohop** — modify `MeshPacket.hop_limit` to 0 in-flight, then deliver. Default behaviour for the standard public channel presets.
- **Drop** — deny a publish entirely so EMQX never delivers it. Off by default; intended for portnum-based spam (e.g. `RANGE_TEST_APP` on the public channels).

Drop runs *before* zerohop. Both are evaluated per message via the EMQX ExHook gRPC interface.

## Architecture

```
Gateway → EMQX → [ExHook gRPC] → floodgate → drop / modify / passthru → EMQX → Subscribers
```

| File | Role |
|------|------|
| `src/floodgate/exhook_server.py` | gRPC server; EMQX connects here. Maps process_message actions to ExHook responses. |
| `src/floodgate/zerohop.py`       | Per-message pipeline: drop check → zerohop check → structured logging. |
| `src/floodgate/config.py`        | Config loader, schema validation (rejects removed keys), policy helpers `should_zerohop` / `should_drop`. |
| `src/floodgate/decrypt.py`       | AES-128-CTR decryption with the Meshtastic default key (drop's portnum lookup needs this for `/e/` packets). |
| `src/floodgate/portnum.py`       | Portnum extraction from `/e/` (decrypt + Data parse) and `/json/` payloads. |
| `src/floodgate/log_setup.py`     | Logging formatter; text and JSON (Loki) modes. |
| `src/floodgate/health.py`        | HTTP health check server on `health_port`. |
| `src/floodgate/__main__.py`      | CLI entry point. |
| `proto/emqx/exhook.proto`        | EMQX ExHook interface definition. |
| `docker-compose.test.yaml`       | Integration test stack (emqx, floodgate, exhook-init, meshtasticd, meshtasticd-init, test-driver) on an isolated bridge network. |
| `scripts/run-integration.sh`     | Integration harness orchestrator — `--keep` leaves the stack up, `--teardown` removes it. |
| `tests/integration/`             | Integration test assets: floodgate config, ExHook init container, meshtasticd init sidecar, test-driver image + cases. |

## Dev Setup

```bash
git clone https://github.com/eric-becker/floodgate
cd floodgate
./scripts/download_protobufs.sh   # Meshtastic protobufs → protobufs/
./scripts/generate_protos.sh      # Python stubs → generated/
pip install -e ".[dev]"
pytest tests/ --ignore=tests/test_container_smoke.py -q   # no Docker required
pytest tests/ -q   # full suite including container smoke test (requires Docker)
./scripts/run-integration.sh   # full Docker Compose end-to-end test (requires Docker)
```

Routing-logic tests mock the low-level zerohop functions, so the suite runs
without Meshtastic protobufs (handy for fast iteration). Tests that exercise
real protobuf payloads (under `tests/payloads/protobuf/`, plus the decrypt
and portnum tests that build synthetic envelopes) skip automatically via
`pytest.importorskip("meshtastic")` when the generated stubs aren't present.
CI generates the stubs before running tests, so the full suite runs there.

## Running

```bash
floodgate --config config.yaml
floodgate --config config.yaml -v   # verbose DEBUG logging (decode steps, gRPC calls)
```

Per-message outcomes (`[ZEROHOP]`, `[PASSTHRU]`, `[NOOP]`, `[DROPPED]`) are logged at **INFO** level. No
special flag needed to see them. `-v` enables DEBUG for internal decode and gRPC detail.

Or with Docker Compose (includes EMQX):
```bash
docker compose up --build
```

After startup, register the ExHook in EMQX (see README Deployment section for the curl commands).

## Branching & Release Strategy

- `main` — stable, tagged releases only
- `feat/<name>`, `fix/<name>`, `docs/<name>`, `chore/<name>` — branch from main, PR back to main
- Commits: conventional format — `feat:`, `fix:`, `docs:`, `test:`, `chore:`
- PRs: squash-merge to main; delete branch after merge
- Releases: `git tag vX.Y.Z && git push origin vX.Y.Z` triggers release + Docker publish to GHCR
- See [CONTRIBUTING.md](CONTRIBUTING.md) for full release workflow details

## Key Config Options

Drop runs **before** zerohop. A packet matched by drop is denied entirely and never reaches the zerohop check.

| Key | Default | Effect |
|-----|---------|--------|
| `zerohop_enabled` | `true` | Master switch for the zero-hop modifier. |
| `zerohop_channels` | 8 standard presets | Channels whose packets get `hop_limit` zeroed. |
| `drop_enabled` | `false` | Master switch for the drop filter. |
| `drop_channels` | `"zerohop_channels"` | Drop scope. List of channels, the literal string `"zerohop_channels"` to inherit, or `null` for all channels. |
| `drop_portnums` | `[]` | Meshtastic portnums (proto enum names like `RANGE_TEST_APP`) to drop. Only readable on default-key (`AQ==`) protobuf channels and on JSON channels. |
| `log_level` | `INFO` | `INFO` logs per-message outcomes. `DEBUG` adds decode/gRPC internals. |
| `log_format` | `text` | `text` (default) or `json` for Loki/Grafana. Override with `FLOODGATE_LOG_FORMAT` env var. |
| `stats_log` | `true` | Log periodic stats summaries. Set `false` to disable. |

The pre-rename keys `channel_policy` / `channel_blacklist` / `channel_whitelist` were removed. floodgate refuses to load a config containing any of them and prints the new equivalents.

## Documentation and Test Discipline

When making code changes, **always update in the same PR**:

- `README.md` — any changed behaviour, config options, log output examples, or CLI flags
- `CLAUDE.md` — file table, config table, and running instructions
- `config.yaml` — if new config keys are added
- `tests/` — add or update tests for changed behaviour before the PR is created
- `CONTRIBUTING.md` — if CI jobs, test tiers, or branch strategy changes

Do not open a PR until README and tests reflect the code changes being merged.

## Local-only paths (never commit)

These paths exist in working trees but are gitignored. Never stage them:

- `.claude/` — local Claude Code settings
- `docs/superpowers/` — planning artifacts written by Claude's superpowers skills
- `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `coverage*` — Python build/test artefacts
- `generated/` — Python protobuf stubs (built by `scripts/generate_protos.sh`)
- `protobufs/meshtastic/` — upstream Meshtastic protobufs (downloaded by `scripts/download_protobufs.sh`); only `protobufs/.gitkeep` and `protobufs/README.md` are tracked

Before starting work on a branch, `git fetch origin && git rebase origin/main` to pick up any landed changes to `.gitignore` or shared workflows.
