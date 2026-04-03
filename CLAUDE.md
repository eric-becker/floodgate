# floodgate

Zero-hop MQTT anti-flood service for Meshtastic. Intercepts MQTT PUBLISH events via
EMQX ExHook (gRPC) and sets `MeshPacket.hop_limit=0` in-flight before delivery to subscribers.

## Architecture

```
Gateway → EMQX → [ExHook gRPC] → floodgate → modified payload → EMQX → Subscribers
```

| File | Role |
|------|------|
| `src/floodgate/exhook_server.py` | gRPC server; EMQX connects here |
| `src/floodgate/zerohop.py` | Core logic: packet decode, zero-hop, structured logging |
| `src/floodgate/config.py` | Config loader; channel policy evaluation |
| `src/floodgate/log_setup.py` | Logging formatter; text and JSON (Loki) modes |
| `src/floodgate/health.py` | HTTP health check server on `health_port` |
| `src/floodgate/__main__.py` | CLI entry point |
| `proto/emqx/exhook.proto` | EMQX ExHook interface definition |

## Dev Setup

```bash
git clone https://github.com/eric-becker/floodgate
cd floodgate
./scripts/download_protobufs.sh   # Meshtastic protobufs → protobufs/
./scripts/generate_protos.sh      # Python stubs → generated/
pip install -e ".[dev]"
pytest tests/ --ignore=tests/test_container_smoke.py -q   # no Docker required
pytest tests/ -q   # full suite including container smoke test (requires Docker)
```

Tests mock protobuf imports — no protobufs needed to run the unit test suite.

## Running

```bash
floodgate --config config.yaml
floodgate --config config.yaml -v   # verbose DEBUG logging (decode steps, gRPC calls)
```

Per-message outcomes (`[ZEROHOP]`, `[PASSTHRU]`, `[NOOP]`) are logged at **INFO** level. No
special flag needed to see them. `-v` enables DEBUG for internal decode and gRPC detail.

Or with Docker Compose (includes EMQX):
```bash
docker compose up --build
```

After startup, register the ExHook in EMQX (see docker-compose.yaml header for the curl command).

## Branching & Release Strategy

- `main` — stable, tagged releases only
- `feat/<name>`, `fix/<name>`, `docs/<name>`, `chore/<name>` — branch from main, PR back to main
- Commits: conventional format — `feat:`, `fix:`, `docs:`, `test:`, `chore:`
- PRs: squash-merge to main; delete branch after merge
- Releases: `git tag vX.Y.Z && git push origin vX.Y.Z` triggers release + Docker publish to GHCR
- See [CONTRIBUTING.md](CONTRIBUTING.md) for full release workflow details

## Key Config Options

| Key | Default | Effect |
|-----|---------|--------|
| `channel_policy` | `blacklist` | `blacklist` (default): zero-hop only listed channels. `whitelist`: zero-hop all except listed. |
| `channel_blacklist` | 8 standard presets | Channels to zero-hop (blacklist mode). Default = all standard Meshtastic public presets. |
| `channel_whitelist` | `[]` | Channels exempt from zero-hop (whitelist mode). Empty = zero-hop everything. |
| `log_level` | `INFO` | `INFO` logs per-message outcomes. `DEBUG` adds decode/gRPC internals. |
| `log_format` | `text` | `text` (default) or `json` for Loki/Grafana. Override with `FLOODGATE_LOG_FORMAT` env var. |
| `stats_log` | `true` | Log periodic stats summaries. Set `false` to disable. |

## Documentation and Test Discipline

When making code changes, **always update in the same PR**:

- `README.md` — any changed behaviour, config options, log output examples, or CLI flags
- `CLAUDE.md` — file table, config table, and running instructions
- `config.yaml` — if new config keys are added
- `tests/` — add or update tests for changed behaviour before the PR is created
- `CONTRIBUTING.md` — if CI jobs, test tiers, or branch strategy changes

Do not open a PR until README and tests reflect the code changes being merged.
