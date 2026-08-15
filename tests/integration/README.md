# Integration test harness

End-to-end harness using Docker Compose. See `CONTRIBUTING.md` ("Integration testing") for usage. Top-level entry point: `scripts/run-integration.sh`. Stack defined in `docker-compose.test.yaml`.

## Case sets

`run.py` selects its cases from the `CASE_SET` environment variable, because the failure-path
cases need floodgate to be *not running* — a state the driver can't create from inside the
network. `scripts/run-integration.sh` drives all three passes in order.

| `CASE_SET` | floodgate | Cases |
|---|---|---|
| `default` | healthy | `zerohop`, `drop`, `passthru`, `noop`, `custom-key-passthru` |
| `hook-down` | **stopped** | `hook-down-fails-open` |
| `recovery` | restarted | `recovery-after-hook-down` |

The script stops floodgate between passes, then starts it again and waits for `/health` plus a
beat for EMQX's 5s `auto_reconnect` before asserting recovery.

## Why the failure-path pass exists

`failed_action: deny` reads like "if floodgate is down, refuse the publish". For
`message.publish` it does not behave that way — an unreachable server makes the gRPC call raise,
`emqx_hooks:safe_execute/2` swallows the exception, and the message is delivered **unmodified**.
floodgate fails *open*: the broker stays up and the mesh silently reverts to full flooding.

`hook-down-fails-open` pins that behavior, including that `hop_limit`/`hop_start` come through
untouched — asserting mere delivery would still pass if EMQX zeroed the hops some other way, and
the difference between "traffic flows" and "protection is off" is the whole point. If a future
EMQX release starts failing *closed*, this case fails and tells you the blast radius of an outage
has changed.

## Gotchas

- **`test-driver` is behind the `driver` compose profile**, so `docker compose up --build` skips
  it and a later `run` silently reuses a stale image. `run-integration.sh` builds it explicitly.
  If driver changes seem to have no effect, this is why.
- **Pass `CASE_SET` via `docker compose run -e`**, not as a shell prefix — compose-file
  interpolation of `${CASE_SET}` does not reliably reach the container here, and the failure mode
  is silent: the default cases run instead, then fail confusingly against a stopped floodgate.
- The `hook-down` and `recovery` passes need `--no-deps`, since `test-driver` declares
  `depends_on: floodgate: service_healthy` and those passes deliberately violate it.
- A failing hook is *slower* than a working one — a dead server refuses immediately, but a hung
  one burns the full `request_timeout` first. `HOOK_FAILURE_SETTLE_SECONDS` (default 9s) covers
  the harness's 5s timeout; raise it if you raise `request_timeout`.
