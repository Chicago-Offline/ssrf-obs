"""Heartbeat (rfsurvey.status.v1) ingest and observer liveness.

The status topic is the one unsigned input this service accepts, so these
tests care as much about what a heartbeat is NOT allowed to do as about the
happy path.
"""
import gzip
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ssrfobs import ingest, web
from ssrfobs.db import DB

SID = "obs-meshpi"
REG = {SID: "unused-pubkey"}


def status(sid=SID, ts=None, schema="rfsurvey.status.v1", **extra):
    doc = {"station_id": sid, "ts": time.time() if ts is None else ts,
           "schema": schema}
    doc.update(extra)
    return json.dumps(doc).encode()


class StatusIngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DB(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_accepts_enrolled_station(self):
        now = time.time()
        self.assertEqual(
            ingest.handle_status(status(ts=now, pending=0), REG, self.db,
                                 topic_station_id=SID), SID)
        rows = self.db.status_rows()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["ts"], now, places=3)
        self.assertEqual(rows[0]["pending"], 0)
        # Full doc is retained for forward compatibility.
        self.assertEqual(rows[0]["payload"]["schema"], "rfsurvey.status.v1")

    def test_accepts_gzip(self):
        raw = gzip.compress(status())
        self.assertEqual(
            ingest.handle_status(raw, REG, self.db, topic_station_id=SID), SID)

    def test_rejects_unknown_station(self):
        with self.assertRaises(ingest.Reject):
            ingest.handle_status(status(sid="obs-nobody"), REG, self.db,
                                 topic_station_id="obs-nobody")
        self.assertEqual(self.db.status_rows(), [])

    def test_rejects_topic_body_mismatch(self):
        """A station may not publish liveness on another station's behalf."""
        with self.assertRaises(ingest.Reject):
            ingest.handle_status(status(sid=SID), REG, self.db,
                                 topic_station_id="obs-other")
        self.assertEqual(self.db.status_rows(), [])

    def test_rejects_unknown_schema(self):
        with self.assertRaises(ingest.Reject):
            ingest.handle_status(status(schema="rfsurvey.obs.v1"), REG,
                                 self.db, topic_station_id=SID)

    def test_rejects_missing_and_bad_ts(self):
        doc = json.loads(status())
        del doc["ts"]
        with self.assertRaises(ingest.Reject):
            ingest.handle_status(json.dumps(doc).encode(), REG, self.db,
                                 topic_station_id=SID)
        with self.assertRaises(ingest.Reject):
            ingest.handle_status(status(ts="nope"), REG, self.db,
                                 topic_station_id=SID)

    def test_rejects_non_object(self):
        with self.assertRaises(ingest.Reject):
            ingest.handle_status(b"[1,2,3]", REG, self.db,
                                 topic_station_id=SID)

    def test_retained_replay_does_not_clobber_fresh(self):
        """The status topic is retained: the broker replays the last
        heartbeat on every reconnect. An older ts must never overwrite a
        newer one, or a dead station looks current after a reconnect."""
        now = time.time()
        ingest.handle_status(status(ts=now), REG, self.db,
                             topic_station_id=SID)
        # Stale frame arrives late / on reconnect.
        self.assertIsNone(
            ingest.handle_status(status(ts=now - 3600), REG, self.db,
                                 topic_station_id=SID))
        rows = self.db.status_rows()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["ts"], now, places=3)

    def test_newer_heartbeat_replaces(self):
        now = time.time()
        ingest.handle_status(status(ts=now - 300, pending=4), REG, self.db,
                             topic_station_id=SID)
        ingest.handle_status(status(ts=now, pending=0), REG, self.db,
                             topic_station_id=SID)
        rows = self.db.status_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pending"], 0)


class ObserversTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DB(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_enrolled_without_heartbeat_is_offline(self):
        out = web.observers(self.db, REG)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["heartbeat"])
        self.assertFalse(out[0]["online"])
        self.assertIsNone(out[0]["age_s"])

    def test_recent_heartbeat_is_online(self):
        now = time.time()
        ingest.handle_status(status(ts=now), REG, self.db,
                             topic_station_id=SID)
        out = web.observers(self.db, REG, now=now)
        self.assertTrue(out[0]["online"])
        self.assertTrue(out[0]["heartbeat"])
        self.assertLess(out[0]["age_s"], 1)

    def test_stale_heartbeat_is_offline(self):
        """Three missed 5-min heartbeats and we stop vouching for it."""
        now = time.time()
        ingest.handle_status(status(ts=now - web.OBSERVER_ONLINE_S - 1), REG,
                             self.db, topic_station_id=SID)
        out = web.observers(self.db, REG, now=now)
        self.assertFalse(out[0]["online"])
        self.assertTrue(out[0]["heartbeat"])  # we heard from it, just not lately

    def test_boundary_is_inclusive(self):
        now = time.time()
        ingest.handle_status(status(ts=now - web.OBSERVER_ONLINE_S), REG,
                             self.db, topic_station_id=SID)
        self.assertTrue(web.observers(self.db, REG, now=now)[0]["online"])

    def test_clock_skew_is_surfaced_not_hidden(self):
        """Station clock ahead of ours: still online, but skew is reported
        so a bad clock reads as a clock problem, not as silent weirdness."""
        now = time.time()
        ingest.handle_status(status(ts=now + 120), REG, self.db,
                             topic_station_id=SID, received_at=now)
        out = web.observers(self.db, REG, now=now)
        self.assertTrue(out[0]["online"])
        self.assertAlmostEqual(out[0]["skew_s"], -120, places=0)

    def test_heartbeat_creates_no_evidence(self):
        """The whole point of the unsigned-input boundary: a heartbeat must
        not make the service claim anything was heard or checked."""
        ingest.handle_status(status(), REG, self.db, topic_station_id=SID)
        self.assertIsNone(web.last_observation_ts(self.db))
        self.assertEqual(web.active(self.db), [])
        st = web.stations(self.db, REG)[0]
        self.assertEqual(st["observations"], 0)
        self.assertEqual(st["sweep_bins"], 0)
        self.assertIsNone(st["last_batch"])


if __name__ == "__main__":
    unittest.main()
