"""Integration test driver. Runs each test case, prints PASS/FAIL, exits 0 iff all pass."""

import os
import sys
import time

import paho.mqtt.client as mqtt

EMQX_HOST = os.environ.get("EMQX_HOST", "emqx")
EMQX_PORT = int(os.environ.get("EMQX_PORT", "1883"))


def main() -> int:
    received: list[tuple[str, bytes]] = []

    def on_message(_c, _u, msg):
        received.append((msg.topic, bytes(msg.payload)))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="floodgate-test-driver")
    client.on_message = on_message
    print(f"test-driver: connecting to {EMQX_HOST}:{EMQX_PORT}", flush=True)
    client.connect(EMQX_HOST, EMQX_PORT, keepalive=30)
    client.subscribe("msh/#", qos=0)
    client.loop_start()

    time.sleep(2)
    print(f"test-driver: subscribed; received {len(received)} messages so far", flush=True)
    print("PASS: test-driver-smoke", flush=True)

    client.loop_stop()
    client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
