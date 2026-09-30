"""Decode, verify, dedupe, store — one envelope at a time."""
import gzip
import json
import logging

from . import verify as verify_mod

log = logging.getLogger(__name__)


class Reject(Exception):
    pass


# Heartbeats are not evidence. See db.SCHEMA station_status for the full
# rationale; the short version is that rf-survey publishes them unsigned, so
# everything below is a station's unverifiable claim about itself. We gate on
# the registry to keep unknown station_ids out of the table, but that is an
# enrollment filter, not authentication -- anyone who can publish to the
# broker can forge one. Never promote a status row into an air claim.
STATUS_SCHEMA_PREFIX = "rfsurvey.status."


def handle_status(payload, registry, db, topic_station_id=None,
                  received_at=None):
    """Ingest one heartbeat. Returns station_id on store, None if skipped.

    topic_station_id is the <station_id> segment of
    <prefix>/status/<station_id>. When supplied it must agree with the body,
    so a station cannot publish liveness on another station's behalf without
    also controlling that station's topic.
    """
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    doc = json.loads(payload)
    if not isinstance(doc, dict):
        raise Reject("status payload is not an object")
    sid = doc.get("station_id")
    if not sid:
        raise Reject("status with no station_id")
    if topic_station_id is not None and topic_station_id != sid:
        raise Reject(
            f"status station_id {sid!r} does not match topic "
            f"{topic_station_id!r}")
    if sid not in registry:
        raise Reject(f"status from unknown station: {sid}")
    schema = str(doc.get("schema", ""))
    if not schema.startswith(STATUS_SCHEMA_PREFIX):
        raise Reject(f"unknown status schema: {schema or None}")
    ts = doc.get("ts")
    if ts is None:
        raise Reject(f"status from {sid} has no ts")
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        raise Reject(f"status from {sid} has non-numeric ts: {ts!r}")
    if db.upsert_status(sid, ts, doc, received_at=received_at):
        log.info("status from %s (ts=%.0f, pending=%s)",
                 sid, ts, doc.get("pending"))
        return sid
    # Retained replay or out-of-order delivery. Expected, not an error.
    log.debug("stale status from %s (ts=%.0f) ignored", sid, ts)
    return None


def handle(payload, registry, db):
    """payload: raw MQTT bytes (gzip JSON envelope). Returns batch_id or raises."""
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    env = json.loads(payload)
    sid = env.get("station_id")
    pub = registry.get(sid)
    if not pub:
        raise Reject(f"unknown station: {sid}")
    if env.get("batch", {}).get("station_id") != sid:
        raise Reject(f"station_id mismatch in envelope from {sid}")
    if not verify_mod.verify(env, pub):
        raise Reject(f"bad signature from {sid}")
    batch = env["batch"]
    if not str(batch.get("schema", "")).startswith("rfsurvey.obs."):
        raise Reject(f"unknown schema: {batch.get('schema')}")
    if db.insert_batch(batch):
        log.info("stored batch %s from %s: %d summaries, %d observations",
                 batch["batch_id"], sid,
                 len(batch.get("sweep_summaries", [])),
                 len(batch.get("observations", [])))
    else:
        log.info("duplicate batch %s from %s ignored", batch["batch_id"], sid)
    return batch["batch_id"]
