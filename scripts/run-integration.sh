#!/usr/bin/env bash
# Local + CI driver for the integration harness.
#
# Modes:
#   (default)    bring stack up, run all cases, tear stack down, exit 0/non-zero
#   --keep       bring stack up, run all cases, leave stack running for poking
#   --teardown   tear the stack down (and volumes/networks); skip running cases

set -euo pipefail

COMPOSE_FILE="docker-compose.test.yaml"
HEALTH_URL="http://localhost:18089/health"
EMQX_URL="http://localhost:18083/api/v5/status"

usage() {
    cat <<EOF
Usage: $0 [--keep | --teardown]
  --keep        Run cases, then leave stack running.
  --teardown    Tear stack down (no cases run).
  (no args)     Run cases and tear stack down on exit.
EOF
}

mode="run-and-teardown"
case "${1:-}" in
    --keep)     mode="run-and-keep" ;;
    --teardown) mode="teardown-only" ;;
    -h|--help)  usage; exit 0 ;;
    "")         ;;
    *)          usage; exit 2 ;;
esac

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

teardown() {
    echo "==> Tearing down integration stack"
    docker compose -f "$COMPOSE_FILE" down -v --remove-orphans
}

if [ "$mode" = "teardown-only" ]; then
    teardown
    exit 0
fi

echo "==> Bringing up integration stack"
docker compose -f "$COMPOSE_FILE" up -d --build
# test-driver is behind the "driver" profile, so the line above skips it and
# `run` would silently reuse a stale image from a previous checkout.
docker compose -f "$COMPOSE_FILE" build test-driver

cleanup_on_error() {
    rc=$?
    if [ $rc -ne 0 ] && [ "$mode" != "run-and-keep" ]; then
        echo "==> Run failed — collecting service logs before teardown"
        docker compose -f "$COMPOSE_FILE" logs --no-color --tail=200 || true
        teardown
    fi
    exit $rc
}
trap cleanup_on_error EXIT

echo "==> Waiting for EMQX REST"
for i in $(seq 1 60); do
    if curl -sf --connect-timeout 2 --max-time 5 -o /dev/null "$EMQX_URL"; then
        echo "    EMQX REST ready after ${i}s"; break
    fi
    sleep 1
    [ "$i" -eq 60 ] && { echo "EMQX REST never came up" >&2; exit 1; }
done

echo "==> Waiting for floodgate /health"
for i in $(seq 1 60); do
    if curl -sf --connect-timeout 2 --max-time 5 -o /dev/null "$HEALTH_URL"; then
        echo "    floodgate /health ready after ${i}s"; break
    fi
    sleep 1
    [ "$i" -eq 60 ] && { echo "floodgate /health never came up" >&2; exit 1; }
done

echo "==> Running test-driver (steady state)"
set +e
docker compose -f "$COMPOSE_FILE" run --rm -e CASE_SET=default test-driver
rc=$?
set -e

# ---------------------------------------------------------------------------
# Failure-path pass: what actually happens to mesh traffic when floodgate dies.
#
# Stopping floodgate rather than pausing it exercises the harsher path — the
# gRPC connection breaks outright instead of hanging. Both fail open; stopping
# is faster and deterministic. --no-deps is required because the test-driver
# declares depends_on floodgate:service_healthy, which we are deliberately
# violating.
# ---------------------------------------------------------------------------
if [ $rc -eq 0 ]; then
    echo "==> Stopping floodgate to exercise the hook-failure path"
    docker compose -f "$COMPOSE_FILE" stop floodgate >/dev/null
    # Let EMQX notice the gRPC channel is gone before publishing.
    sleep 3

    set +e
    docker compose -f "$COMPOSE_FILE" run --rm --no-deps -e CASE_SET=hook-down test-driver
    rc=$?
    set -e
    echo "==> hook-down exit code: $rc"

    echo "==> Restarting floodgate"
    docker compose -f "$COMPOSE_FILE" start floodgate >/dev/null
    for i in $(seq 1 60); do
        if curl -sf --connect-timeout 2 --max-time 5 -o /dev/null "$HEALTH_URL"; then
            echo "    floodgate /health ready again after ${i}s"; break
        fi
        sleep 1
        [ "$i" -eq 60 ] && { echo "floodgate never came back" >&2; exit 1; }
    done
    # EMQX auto_reconnect is 5s; give it a beat to re-establish before asserting.
    sleep 8

    if [ $rc -eq 0 ]; then
        set +e
        docker compose -f "$COMPOSE_FILE" run --rm --no-deps -e CASE_SET=recovery test-driver
        rc=$?
        set -e
        echo "==> recovery exit code: $rc"
    fi
fi

echo "==> Test-driver exit code: $rc"

if [ "$mode" = "run-and-keep" ]; then
    echo "==> --keep: leaving stack running."
    echo "    floodgate /health: $HEALTH_URL"
    echo "    EMQX dashboard:    http://localhost:18083 (admin/public)"
    echo "    Tear down with:    $0 --teardown"
    trap - EXIT
    exit $rc
fi

trap - EXIT

if [ $rc -ne 0 ]; then
    # Dump service logs BEFORE teardown on the explicit-failure path. Without
    # this, the workflow's `if: failure()` log-dump step fires after teardown
    # finishes — by then all containers are gone and nothing remains to log.
    echo "==> Run failed — dumping service logs before teardown"
    docker compose -f "$COMPOSE_FILE" logs --no-color --tail=400 || true
fi
teardown
exit $rc
