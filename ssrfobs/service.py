"""MQTT subscriber loop."""
import logging
import os

import paho.mqtt.client as mqtt

from . import ingest

log = logging.getLogger(__name__)


def run(cfg, registry, db):
    m = cfg["mqtt"]
    prefix = m.get("topic_prefix", "rfsurvey")

    obs_topic = f"{prefix}/obs/#"
    status_topic = f"{prefix}/status/#"

    def on_connect(client, userdata, flags, reason_code, properties=None):
        log.info("connected to %s (%s); subscribing %s and %s",
                 m["server"], reason_code, obs_topic, status_topic)
        client.subscribe(obs_topic, qos=1)
        # qos=0 to match rf-survey's Publisher.heartbeat(). The topic is
        # retained, so every reconnect replays the last heartbeat per
        # station; ingest.handle_status drops the replay on its ts compare
        # rather than treating arrival as liveness.
        client.subscribe(status_topic, qos=0)

    def _station_from_topic(topic, kind):
        """Trailing segment of <prefix>/<kind>/<station_id>, else None."""
        head = f"{prefix}/{kind}/"
        if not topic.startswith(head):
            return None
        rest = topic[len(head):]
        return rest.split("/", 1)[0] or None

    def on_message(client, userdata, msg):
        try:
            if msg.topic.startswith(f"{prefix}/status/"):
                ingest.handle_status(
                    msg.payload, registry, db,
                    topic_station_id=_station_from_topic(msg.topic, "status"))
            else:
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
