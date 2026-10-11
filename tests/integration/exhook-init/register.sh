#!/usr/bin/env bash
# Register floodgate as an ExHook in EMQX once the broker REST API is up.
# Idempotent: if a hook named "floodgate" already exists, PUT to update it
# instead of POSTing a new one.

set -euo pipefail

EMQX_URL="${EMQX_URL:-http://emqx:18083}"
EMQX_USER="${EMQX_USER:-admin}"
EMQX_PASS="${EMQX_PASS:-public}"
HOOK_URL="${HOOK_URL:-http://floodgate:9000}"
HOOK_NAME="floodgate"

echo "exhook-init: waiting for EMQX REST at ${EMQX_URL} ..."
for i in $(seq 1 60); do
    if curl -sf --connect-timeout 2 --max-time 5 -o /dev/null "${EMQX_URL}/api/v5/status"; then
        echo "exhook-init: EMQX REST is up after ${i}s"
        break
    fi
    sleep 1
    [ "$i" -eq 60 ] && { echo "exhook-init: EMQX REST never came up" >&2; exit 1; }
done

echo "exhook-init: logging in"
TOKEN=$(curl -sf --connect-timeout 2 --max-time 5 -X POST "${EMQX_URL}/api/v5/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"${EMQX_USER}\",\"password\":\"${EMQX_PASS}\"}" \
    | jq -r .token)

if [ -z "${TOKEN}" ] || [ "${TOKEN}" = "null" ]; then
    echo "exhook-init: failed to obtain EMQX REST token" >&2
    exit 1
fi

BODY=$(cat <<EOF
{
  "name": "${HOOK_NAME}",
  "url": "${HOOK_URL}",
  "auto_reconnect": "5s",
  "failed_action": "deny",
  "request_timeout": "5s"
}
EOF
)

if curl -sf --connect-timeout 2 --max-time 5 -o /dev/null "${EMQX_URL}/api/v5/exhooks/${HOOK_NAME}" \
    -H "Authorization: Bearer ${TOKEN}"; then
    echo "exhook-init: hook '${HOOK_NAME}' exists — updating"
    curl -sf --connect-timeout 2 --max-time 5 -X PUT "${EMQX_URL}/api/v5/exhooks/${HOOK_NAME}" \
        -H "Authorization: Bearer ${TOKEN}" \
        -H 'Content-Type: application/json' \
        -d "${BODY}" >/dev/null
else
    echo "exhook-init: hook '${HOOK_NAME}' does not exist — creating"
    curl -sf --connect-timeout 2 --max-time 5 -X POST "${EMQX_URL}/api/v5/exhooks" \
        -H "Authorization: Bearer ${TOKEN}" \
        -H 'Content-Type: application/json' \
        -d "${BODY}" >/dev/null
fi

echo "exhook-init: verifying registration"
# EMQX 6 reports connection status per node under node_status[]; there is no
# top-level `status`. Require every node to be connected before the test
# driver starts, so a hook that never connects fails here, not as a confusing
# assertion later.
for attempt in $(seq 1 15); do
    NODES=$(curl -sf --connect-timeout 2 --max-time 5 "${EMQX_URL}/api/v5/exhooks/${HOOK_NAME}" \
        -H "Authorization: Bearer ${TOKEN}" | jq -r '[.node_status[].status] | join(",")' || true)
    echo "exhook-init: attempt ${attempt}: node_status=${NODES:-unreadable}"
    if [ -n "${NODES}" ] && [ -z "$(echo "${NODES}" | tr ',' '\n' | grep -v '^connected$')" ]; then
        echo "exhook-init: success"
        exit 0
    fi
    sleep 2
done

echo "exhook-init: hook '${HOOK_NAME}' never reported connected on every node" >&2
exit 1
