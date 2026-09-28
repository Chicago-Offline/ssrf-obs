"""Decode, verify, dedupe, store — one envelope at a time."""
import gzip
import json
import logging

from . import verify as verify_mod

log = logging.getLogger(__name__)


class Reject(Exception):
    pass


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
