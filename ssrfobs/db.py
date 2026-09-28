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

-- Observer descriptors (rf-survey NETWORK.md S1): additive `receivers`
-- block from the batch. Upserted per (station_id, receiver) — latest
-- descriptor wins, since antenna/placement is current state, not history.
CREATE TABLE IF NOT EXISTS receivers (
  station_id TEXT NOT NULL,
  receiver TEXT NOT NULL,
  role TEXT, gain REAL, antenna TEXT, placement TEXT,
  updated_at REAL NOT NULL,
  PRIMARY KEY (station_id, receiver)
);

-- Beacon calibration readings (rf-survey NETWORK.md S7). Keyed by OBSERVER
-- (station_id, receiver), never by station alone — two dongles on one
-- host can carry different antennas and averaging them would smear
-- exactly the hardware variance these readings exist to catch.
CREATE TABLE IF NOT EXISTS beacon_readings (
  id INTEGER PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES batches(id),
  station_id TEXT NOT NULL,
  receiver TEXT NOT NULL,
  ts REAL NOT NULL,
  ref_id TEXT NOT NULL,
  freq_hz INTEGER NOT NULL,
  band TEXT,
  coverage TEXT NOT NULL,     -- ok | no_reference | unverified
  signal_db REAL, noise_db REAL, snr_db REAL,
  gain REAL, pinned INTEGER DEFAULT 0,
  status TEXT NOT NULL        -- ok | not_heard | no_reference | error
);
CREATE INDEX IF NOT EXISTS beacon_rx ON beacon_readings(station_id, receiver, ref_id, ts);
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
        for r in batch.get("receivers", []) or []:
            if not r.get("receiver"):
                continue
            self.db.execute(
                "INSERT INTO receivers(station_id,receiver,role,gain,antenna,"
                "placement,updated_at) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(station_id,receiver) DO UPDATE SET"
                " role=excluded.role, gain=excluded.gain,"
                " antenna=excluded.antenna, placement=excluded.placement,"
                " updated_at=excluded.updated_at",
                (batch["station_id"], r["receiver"], r.get("role"),
                 r.get("gain"),
                 json.dumps(r["antenna"]) if r.get("antenna") else None,
                 json.dumps(r["placement"]) if r.get("placement") else None,
                 time.time()))
        self.db.executemany(
            "INSERT INTO beacon_readings(batch_id,station_id,receiver,ts,"
            "ref_id,freq_hz,band,coverage,signal_db,noise_db,snr_db,gain,"
            "pinned,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(batch["batch_id"], batch["station_id"], b.get("receiver"),
              b.get("ts"), b.get("ref_id"), b.get("freq_hz"), b.get("band"),
              b.get("coverage"), b.get("signal_db"), b.get("noise_db"),
              b.get("snr_db"), b.get("gain"), 1 if b.get("pinned") else 0,
              b.get("status"))
             for b in batch.get("beacon_readings", []) or []])
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

    def receiver_rows(self):
        """Latest observer descriptor per (station_id, receiver)."""
        cols = ("station_id", "receiver", "role", "gain", "antenna",
                "placement", "updated_at")
        rows = self.db.execute(
            "SELECT station_id,receiver,role,gain,antenna,placement,"
            "updated_at FROM receivers").fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def beacon_latest(self):
        """Most recent reading per (station_id, receiver, ref_id)."""
        cols = ("station_id", "receiver", "ref_id", "freq_hz", "band",
                "coverage", "signal_db", "noise_db", "snr_db", "gain",
                "pinned", "status", "ts")
        rows = self.db.execute(
            "SELECT b.station_id,b.receiver,b.ref_id,b.freq_hz,b.band,"
            "b.coverage,b.signal_db,b.noise_db,b.snr_db,b.gain,b.pinned,"
            "b.status,b.ts FROM beacon_readings b JOIN (SELECT station_id,"
            "receiver,ref_id,MAX(ts) t FROM beacon_readings GROUP BY "
            "station_id,receiver,ref_id) m ON m.station_id=b.station_id"
            " AND m.receiver=b.receiver AND m.ref_id=b.ref_id AND m.t=b.ts"
            " ORDER BY b.station_id,b.receiver,b.freq_hz").fetchall()
        return [dict(zip(cols, r)) for r in rows]
