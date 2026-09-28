"""MQTT subscriber loop."""
import logging
import os

import paho.mqtt.client as mqtt

from . import ingest

log = logging.getLogger(__name__)


def run(cfg, registry, db):
    m = cfg["mqtt"]
    prefix = m.get("topic_prefix", "rfsurvey")

    def on_connect(client, userdata, flags, reason_code, properties=None):
        log.info("connected to %s (%s); subscribing %s/obs/#",
                 m["server"], reason_code, prefix)
        client.subscribe(f"{prefix}/obs/#", qos=1)

    def on_message(client, userdata, msg):
        try:
            ingest.handle(msg.payload, registry, db)
        except ingest.Reject as e:
            log.warning("rejected on %s: %s", msg.topic, e)
        except Exception:
            log.exception("error handling message on %s", msg.topic)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=cfg.get("client_id", "ssrf-obs"),
                         transport=m.get("transport", "tcp"))
    if m.get("tls"):
        client.tls_set()
    user = m.get("username")
    pw = m.get("password") or os.environ.get("SSRF_OBS_MQTT_PASSWORD")
    if user or pw:
        client.username_pw_set(user, pw)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(m["server"], int(m.get("port", 1883)), keepalive=60)
    client.loop_forever(retry_first_connection=True)
