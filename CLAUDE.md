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
| `src/floodgate/antiflood.py` | Core logic: packet decode, zerohop, logging |
| `src/floodgate/config.py` | Config loader; channel policy evaluation |
| `src/floodgate/__main__.py` | CLI entry point |
| `proto/emqx/exhook.proto` | EMQX ExHook interface definition |

## Dev Setup

```bash
git clone https://github.com/eric-becker/floodgate
cd floodgate
./scripts/download_protobufs.sh   # Meshtastic protobufs → protobufs/
./scripts/generate_protos.sh      # Python stubs → generated/
pip install -e ".[dev]"
pytest tests/ -q
```

Tests mock protobuf imports — no protobufs needed to run tests.

## Running

```bash
floodgate --config config.yaml
floodgate --config config.yaml -v   # DEBUG per-message logging
```

Or with Docker Compose (includes EMQX):
```bash
docker compose up --build
```

After startup, register the ExHook in EMQX (see docker-compose.yaml header for the curl command).

## Branching & Commit Strategy

- `main` — stable, tagged releases only
- `feat/<name>`, `fix/<name>`, `docs/<name>` — branch from main, PR back to main
- Commits: conventional format — `feat:`, `fix:`, `docs:`, `test:`, `chore:`
- PRs: squash-merge to main
- Releases: semantic versioning (`v0.1.0`, ...), GitHub release + git tag

## Key Config Options

| Key | Default | Effect |
|-----|---------|--------|
| `channel_policy` | `whitelist` | `whitelist`: zerohop all except listed. `blacklist`: zerohop only listed. |
| `channel_whitelist` | `[]` | Channels exempt from zerohop (whitelist mode). Empty = zerohop everything. |
| `channel_blacklist` | standard presets | Channels to zerohop (blacklist mode). |
| `log_level` | `INFO` | Set to `DEBUG` for per-message outcome logs. |
