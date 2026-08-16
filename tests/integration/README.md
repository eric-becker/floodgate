# Integration test harness

End-to-end harness using Docker Compose. Entry point: `scripts/run-integration.sh`. Stack:
`docker-compose.test.yaml`. Usage in `CONTRIBUTING.md`.

```
                    ┌──────────┐   ExHook gRPC   ┌───────────┐
  test-driver ─────►│   EMQX   │◄───────────────►│ floodgate │
   (publish)        └────┬─────┘                 └───────────┘
                         │ MQTT
                         ▼
                    ┌──────────┐   real Meshtastic firmware
                    │ mesh-sim │   (portduino, no radio)
                    └──────────┘
```

Everything runs on the private `floodgate-test-net` bridge. Only `18083` (EMQX) and `18089`
(floodgate health) reach localhost — **nothing can reach a real broker.**

## Passes

`run-integration.sh` runs four passes in order, selecting cases via `CASE_SET`:

| `CASE_SET` | floodgate | Asserts |
|---|---|---|
| `default` | up | zerohop, drop, passthru, noop, custom-key-passthru |
| `hook-down` | **stopped** | packet delivered **unmodified**, `metrics.failed` rises |
| `recovery` | restarted | zerohop resumes via `auto_reconnect` |
| `firmware` | up | a real node **accepts** the zero-hopped packet; traceroute has **no ghost hops** |

## Why the last two passes exist

**`hook-down`** — `failed_action: deny` reads like "refuse the publish if floodgate is down". It
does not behave that way: an unreachable server makes the gRPC call raise,
`emqx_hooks:safe_execute/2` swallows it, and the message is delivered untouched. floodgate fails
**open** — the broker stays up and the mesh silently reverts to full flooding. If a future EMQX
release fails *closed*, this pass tells you the blast radius changed.

**`firmware`** — unit tests can only check fields we thought to check. A real node applies its own
admission rules, so a packet can be structurally perfect and still be discarded on arrival:

```
classifyHopStart():  hop_start == 0  ─┬─ decoded Data has bitfield → VALID
                                      └─ otherwise                 → DROPPED
```

Firmware ≥ 2.5.0 always sets that bitfield, so real traffic is fine — but a synthetic packet
without it is dropped, and no unit test models this. Ghost hops come from the same counters:
`TraceRouteModule::insertUnknownHops` pads the route with `NODENUM_BROADCAST` ("ffff") once per hop
that `hop_start - hop_limit` claims but the route doesn't record. Zeroing `hop_limit` alone left
`hop_start` intact, so every packet claimed the full hop limit. Revert that and the case fails with
3 ghosts — verified, not assumed.

Deliberately **one portnum**: portnum handling is covered by the unit suite, and admission is
portnum-independent. More cases here would add setup, not signal.

## Gotchas

- **`mesh-sim` config does not survive a container recreate** (no volume). `run-integration.sh`
  always configures it after bringing the stack up; if you `compose up` by hand, reconfigure.
- **MQTT needs a restart to start, and a channel to exist at all.** `wantsLink()` in
  `src/mqtt/MQTT.cpp` requires `channels.anyMqttEnabled()` — without uplink/downlink on a channel
  the node logs `Init MQTT` and never connects.
- **`MeshPacket.channel` (the channel hash) must be set** for firmware cases. floodgate ignores it,
  so the ExHook-level cases leave it 0; firmware rejects that with `Invalid channel index`.
- **The node's number is derived, never hardcoded** — it falls out of the hwid and erase state, so
  a pinned constant addresses the wrong node and directed messages just time out.
- **`test-driver` is behind the `driver` compose profile**, so `up --build` skips it and `run`
  reuses a stale image. The script builds it explicitly.
- **Pass `CASE_SET` via `run -e`**, not a shell prefix — compose-file `${VAR}` interpolation does
  not reach the container, and the default cases run instead.
- `mesh-sim` is **pinned by digest**. `daily-*` moves, and a moving dependency turns unrelated PRs
  red. Bump it deliberately.
