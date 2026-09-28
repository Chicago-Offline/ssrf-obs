"""Append-only evidence store. Deliberately outside ssrf-lite (NETWORK.md §3)."""
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  generated_at REAL,
  received_at REAL NOT NULL,
  schema TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sweep_summaries (
  batch_id TEXT NOT NULL REFERENCES batches(id),
  station_id TEXT NOT NULL,
  receiver TEXT NOT NULL,
  freq_hz INTEGER NOT NULL,
  median_db REAL, max_db REAL, hits INTEGER
);
CREATE INDEX IF NOT EXISTS ss_freq ON sweep_summaries(freq_hz);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES batches(id),
  station_id TEXT NOT NULL,
  src_id INTEGER,
  ts REAL, receiver TEXT, freq_hz INTEGER,
  snr_db REAL, duration_s REAL, decoder TEXT, gated INTEGER,
  meta TEXT, lat REAL, lon REAL, alt_m REAL, fix TEXT
);
CREATE INDEX IF NOT EXISTS obs_freq ON observations(freq_hz);
CREATE INDEX IF NOT EXISTS obs_station ON observations(station_id);
"""


class DB:
    def __init__(self, path):
        path = os.path.expanduser(path)
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)

    def has_batch(self, batch_id):
        return self.db.execute("SELECT 1 FROM batches WHERE id=?",
                               (batch_id,)).fetchone() is not None

    def insert_batch(self, batch):
        """Idempotent by batch UUID — replays are no-ops."""
        if self.has_batch(batch["batch_id"]):
            return False
        self.db.execute(
            "INSERT INTO batches(id,station_id,generated_at,received_at,schema)"
            " VALUES(?,?,?,?,?)",
            (batch["batch_id"], batch["station_id"], batch.get("generated_at"),
             time.time(), batch.get("schema", "?")))
        self.db.executemany(
            "INSERT INTO sweep_summaries(batch_id,station_id,receiver,freq_hz,"
            "median_db,max_db,hits) VALUES(?,?,?,?,?,?,?)",
            [(batch["batch_id"], batch["station_id"], s["receiver"], s["freq_hz"],
              s.get("median_db"), s.get("max_db"), s.get("hits"))
             for s in batch.get("sweep_summaries", [])])
        self.db.executemany(
            "INSERT INTO observations(batch_id,station_id,src_id,ts,receiver,"
            "freq_hz,snr_db,duration_s,decoder,gated,meta,lat,lon,alt_m,fix)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(batch["batch_id"], batch["station_id"], o.get("id"), o.get("ts"),
              o.get("receiver"), o.get("freq_hz"), o.get("snr_db"),
              o.get("duration_s"), o.get("decoder"),
              1 if o.get("gated") else 0, json.dumps(o.get("meta") or {}),
              o.get("lat"), o.get("lon"), o.get("alt_m"), o.get("fix"))
             for o in batch.get("observations", [])])
        self.db.commit()
        return True

    def observation_rows(self, freq_hz=None):
        q = ("SELECT station_id, ts, freq_hz, snr_db, decoder, gated, meta"
             " FROM observations")
        args = []
        if freq_hz:
            q += " WHERE freq_hz=?"
            args.append(freq_hz)
        return self.db.execute(q, args).fetchall()
