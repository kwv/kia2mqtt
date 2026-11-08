"""Entrypoint: python -m kia2mqtt"""

import json
import logging
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

from . import config
from .bridge import Bridge
from .kia import KiaClient

_LOGGER = logging.getLogger("kia2mqtt")


def start_health_server(bridge: Bridge, port: int) -> ThreadingHTTPServer:
    """GET /health: 200 while MQTT is connected, else 503. Body has details."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/health":
                self.send_error(404)
                return
            body = json.dumps(bridge.health()).encode()
            self.send_response(200 if bridge.mqtt_connected else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    try:
        settings = config.load()
    except config.ConfigError as err:
        print(f"kia2mqtt: {err}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The library logs full request and response bodies (VIN, vehicle key,
    # account details) at DEBUG, so keep it at WARNING whatever LOG_LEVEL is.
    logging.getLogger("hyundai_kia_connect_api").setLevel(logging.WARNING)

    mqttc = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"kia2mqtt-{settings.car_name}",
        clean_session=True,  # no queued commands replayed after a reconnect
    )

    def publish(topic: str, payload: str, retain: bool) -> None:
        mqttc.publish(topic, payload, qos=1, retain=retain)

    bridge = Bridge(settings, KiaClient(settings), publish)
    connected = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties):  # noqa: ANN001
        if reason_code.is_failure:
            _LOGGER.error("MQTT connect failed: %s", reason_code)
            return
        _LOGGER.info("MQTT connected")
        client.subscribe([(t, 1) for t in bridge.topics.inbound])
        bridge.on_mqtt_connect()
        connected.set()

    def on_disconnect(client, userdata, flags, reason_code, properties):  # noqa: ANN001
        bridge.mqtt_connected = False
        _LOGGER.warning("MQTT disconnected: %s", reason_code)

    def on_message(client, userdata, msg):  # noqa: ANN001
        bridge.on_mqtt_message(msg.topic, msg.payload, bool(msg.retain))

    mqttc.on_connect = on_connect
    mqttc.on_disconnect = on_disconnect
    mqttc.on_message = on_message
    mqttc.will_set(bridge.topics.online, "false", qos=1, retain=True)
    if settings.mqtt.username:
        mqttc.username_pw_set(settings.mqtt.username, settings.mqtt.password)
    if settings.mqtt.tls:
        mqttc.tls_set()
    mqttc.reconnect_delay_set(min_delay=1, max_delay=60)
    mqttc.connect_async(settings.mqtt.host, settings.mqtt.port, keepalive=60)
    mqttc.loop_start()

    start_health_server(bridge, settings.health_port)

    def stop(signum, frame):  # noqa: ANN001
        bridge.stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    _LOGGER.info(
        "kia2mqtt starting for '%s', polling every %d min",
        settings.car_name,
        settings.poll_minutes,
    )
    # Non-retained events (e.g. otp_required) sent before the first connect
    # would be lost, so give the broker a moment first.
    if not connected.wait(30):
        _LOGGER.warning("MQTT not connected after 30 s; starting anyway")
    try:
        bridge.run()
    finally:
        mqttc.publish(
            bridge.topics.online, "false", qos=1, retain=True
        ).wait_for_publish(5)
        mqttc.loop_stop()
        mqttc.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
