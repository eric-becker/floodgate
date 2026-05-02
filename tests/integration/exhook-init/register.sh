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
STATUS=$(curl -sf --connect-timeout 2 --max-time 5 "${EMQX_URL}/api/v5/exhooks/${HOOK_NAME}" \
    -H "Authorization: Bearer ${TOKEN}" | jq -r '.status // "unknown"')
echo "exhook-init: hook '${HOOK_NAME}' status=${STATUS}"

if [ "${STATUS}" = "connected" ] || [ "${STATUS}" = "running" ]; then
    echo "exhook-init: success"
    exit 0
fi

echo "exhook-init: hook registered but status=${STATUS}; floodgate may still be starting."
echo "exhook-init: init container exits 0; floodgate->EMQX gRPC will recover on its own."
exit 0
