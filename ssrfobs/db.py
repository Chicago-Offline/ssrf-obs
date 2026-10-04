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

-- Monitor checks (rf-survey NETWORK.md S2/S3): one row per target check,
-- INCLUDING the silent ones (heard=0), which are the majority and the
-- whole point. Without a record of having looked and heard nothing, a
-- quiet channel is indistinguishable from one no observer ever visited,
-- so "when was this last heard" and "has anyone ever heard it" are
-- unanswerable. Never collapse these to hearings-only.
--
-- target + ssrf_id are the catalog join keys, and params carries the
-- per-parameter grades from the station-side verify_params(). Neither
-- survives the generic observations path, which is why monitor evidence
-- gets its own table rather than being folded into observations.
--
-- Keyed by OBSERVER (station_id, receiver) for the same reason as
-- beacon_readings: receiver names are station-local and collide across
-- stations (every host has an rtl:0), so counting bare receiver would
-- invent corroboration between two dongles that are actually one.
CREATE TABLE IF NOT EXISTS monitor_checks (
  id INTEGER PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES batches(id),
  station_id TEXT NOT NULL,
  src_id INTEGER,
  ts REAL NOT NULL,
  receiver TEXT NOT NULL,
  target TEXT NOT NULL,
  ssrf_id TEXT,
  freq_hz INTEGER NOT NULL,
  decoder TEXT,
  heard INTEGER NOT NULL,
  snr_db REAL,
  params TEXT, meta TEXT,
  lat REAL, lon REAL, alt_m REAL, fix TEXT
);
CREATE INDEX IF NOT EXISTS mc_chan ON monitor_checks(freq_hz, target, ts);
CREATE INDEX IF NOT EXISTS mc_ssrf ON monitor_checks(ssrf_id);
CREATE INDEX IF NOT EXISTS mc_obs ON monitor_checks(station_id, receiver);

-- Station heartbeats (rf-survey `rfsurvey.status.*`, currently v2, topic
-- <prefix>/status/<station_id>).
--
-- 🔴 UNSIGNED, UNLIKE EVERYTHING ELSE IN THIS FILE. rf-survey's
-- Publisher.heartbeat() publishes bare JSON with no Ed25519 envelope, so
-- these rows carry a station's *claim* about itself that we cannot verify.
-- They are advisory liveness only. NEVER let a status row back an
-- evidence claim -- not "heard", not "checked", not a parameter grade.
-- Anything the page asserts about the air must still come from
-- observations / monitor_checks / beacon_readings.
--
-- Why store them at all: build_batch() returns None when a station has no
-- unsubmitted evidence, so a WEDGED observer (the known rtl_power hang)
-- and a merely quiet band both produce total silence on obs/#. The
-- heartbeat is the only signal that separates "looking, heard nothing"
-- from "not looking". That is worth having even unsigned.
--
-- One row per station, latest wins. Retained-topic hazard: the broker
-- replays the last heartbeat on every reconnect, so arrival time proves
-- nothing about liveness -- ts is the station's own clock and is what
-- freshness must be computed from. received_at is kept only to expose
-- skew between the two.
CREATE TABLE IF NOT EXISTS station_status (
  station_id TEXT PRIMARY KEY,
  ts REAL NOT NULL,           -- station's claimed clock, drives freshness
  received_at REAL NOT NULL,  -- our clock on arrival, for skew only
  pending INTEGER,
  payload TEXT NOT NULL       -- full status doc, forward-compatible
);
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
        self.db.executemany(
            "INSERT INTO monitor_checks(batch_id,station_id,src_id,ts,"
            "receiver,target,ssrf_id,freq_hz,decoder,heard,snr_db,params,"
            "meta,lat,lon,alt_m,fix)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(batch["batch_id"], batch["station_id"], c.get("id"),
              c.get("ts"), c.get("receiver"), c.get("target"),
              c.get("ssrf_id"), c.get("freq_hz"), c.get("decoder"),
              1 if c.get("heard") else 0, c.get("snr_db"),
              json.dumps(c.get("params") or {}),
              json.dumps(c.get("meta") or {}),
              c.get("lat"), c.get("lon"), c.get("alt_m"), c.get("fix"))
             for c in batch.get("monitor_checks", []) or []
             if c.get("target") and c.get("freq_hz") is not None])
        self.db.commit()
        return True

    def upsert_status(self, station_id, ts, payload, received_at=None):
        """Record a heartbeat. Monotonic in ts: an older heartbeat never
        overwrites a newer one.

        That guard is not paranoia. The status topic is retained, so a
        reconnect replays whatever was last published; without the ts
        compare, a stale retained frame from a station that died hours ago
        would clobber a fresh one and make a dead observer look current.
        """
        if ts is None:
            return False
        received_at = time.time() if received_at is None else received_at
        cur = self.db.execute(
            "SELECT ts FROM station_status WHERE station_id=?",
            (station_id,)).fetchone()
        if cur and cur[0] is not None and float(cur[0]) >= float(ts):
            return False
        self.db.execute(
            "INSERT INTO station_status(station_id,ts,received_at,pending,"
            "payload) VALUES(?,?,?,?,?)"
            " ON CONFLICT(station_id) DO UPDATE SET ts=excluded.ts,"
            " received_at=excluded.received_at, pending=excluded.pending,"
            " payload=excluded.payload",
            (station_id, float(ts), float(received_at),
             payload.get("pending"), json.dumps(payload)))
        self.db.commit()
        return True

    def status_rows(self):
        """Latest heartbeat per station. Advisory only -- see schema note."""
        cols = ("station_id", "ts", "received_at", "pending", "payload")
        rows = self.db.execute(
            "SELECT station_id,ts,received_at,pending,payload"
            " FROM station_status").fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            try:
                d["payload"] = json.loads(d["payload"] or "{}")
            except ValueError:
                d["payload"] = {}
            out.append(d)
        return out

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

    def monitor_check_rows(self, freq_hz=None, ssrf_id=None):
        """Raw monitor checks, silent ones included.

        Returns (station_id, receiver, ts, target, ssrf_id, freq_hz,
        decoder, heard, snr_db, params, meta), oldest first.

        No heard=1 filter, deliberately: the silent rows are what make
        "last heard" and "never observed" answerable, so filtering them
        here would defeat the reason the table exists.
        """
        q = ("SELECT station_id,receiver,ts,target,ssrf_id,freq_hz,decoder,"
             "heard,snr_db,params,meta FROM monitor_checks")
        args, where = [], []
        if freq_hz:
            where.append("freq_hz=?")
            args.append(freq_hz)
        if ssrf_id:
            where.append("ssrf_id=?")
            args.append(ssrf_id)
        if where:
            q += " WHERE " + " AND ".join(where)
        return self.db.execute(q + " ORDER BY ts", args).fetchall()

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
