#!/usr/bin/env bash
# Configure MQTT on the meshtasticd container after it is up. MQTT lives in
# ModuleConfig protobuf on the firmware, not in the YAML config file —
# upstream firmware's own CI does the same: start meshtasticd -s, then poke
# settings over the TCP API on port 4403.

set -euo pipefail

HOST="${MESHTASTICD_HOST:-meshtasticd}"
PORT="${MESHTASTICD_PORT:-4403}"
EMQX_ADDR="${EMQX_ADDR:-emqx:1883}"

echo "meshtasticd-init: waiting for ${HOST}:${PORT}"
for i in $(seq 1 60); do
    if nc -z "${HOST}" "${PORT}" 2>/dev/null; then
        echo "meshtasticd-init: TCP API ready after ${i}s"; break
    fi
    sleep 1
    [ $i -eq 60 ] && { echo "meshtasticd-init: ${HOST}:${PORT} never came up" >&2; exit 1; }
done

# Brief settle so firmware finishes its own init before we start poking config
sleep 3

echo "meshtasticd-init: configuring MQTT module"
meshtastic --host "${HOST}" --port "${PORT}" \
    --set moduleConfig.mqtt.enabled            true \
    --set moduleConfig.mqtt.address            "${EMQX_ADDR}" \
    --set moduleConfig.mqtt.root               msh \
    --set moduleConfig.mqtt.tls_enabled        false \
    --set moduleConfig.mqtt.encryption_enabled true \
    --set moduleConfig.mqtt.json_enabled       false

# Let the firmware reconnect to the broker with the new settings
sleep 5

echo "meshtasticd-init: sending probe text message"
meshtastic --host "${HOST}" --port "${PORT}" \
    --sendtext "floodgate-roundtrip-probe"

echo "meshtasticd-init: done"
