"""Heartbeat (rfsurvey.status.v1/v2) ingest and observer liveness.

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


def v2(ts=None, receivers=None, stale_after_s=3600, **extra):
    return status(ts=ts, schema="rfsurvey.status.v2",
                  receivers={} if receivers is None else receivers,
                  stale_after_s=stale_after_s, **extra)


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


class ReceiverHealthTest(unittest.TestCase):
    """v2 receiver liveness: 'online' and 'collecting' are not the same."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DB(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _put(self, payload):
        ingest.handle_status(payload, REG, self.db, topic_station_id=SID)

    def test_v1_reports_unknown_not_broken(self):
        """An old publisher cannot report health; that is not a failure."""
        self._put(status())
        out = web.observers(self.db, REG)[0]
        self.assertTrue(out["online"])
        self.assertIsNone(out["collecting"])
        self.assertIsNone(out["receivers_total"])

    def test_no_heartbeat_reports_unknown_collecting(self):
        out = web.observers(self.db, REG)[0]
        self.assertFalse(out["online"])
        self.assertIsNone(out["collecting"])
        self.assertEqual(out["receivers"], {})

    def test_fresh_receivers_are_collecting(self):
        now = time.time()
        self._put(v2(ts=now, receivers={
            "BENCH": {"last_sweep_ts": now - 30, "last_sweep_age_s": 30},
            "ADSB": {"last_sweep_ts": now - 20, "last_sweep_age_s": 20}}))
        out = web.observers(self.db, REG, now=now)[0]
        self.assertTrue(out["collecting"])
        self.assertEqual(out["receivers_healthy"], 2)
        self.assertEqual(out["receivers_total"], 2)

    def test_online_but_not_collecting(self):
        """meshpi's real failure: submit loop fine, radios dead for days."""
        now = time.time()
        stale = 3 * 86400
        self._put(v2(ts=now, pending=0, receivers={
            "BENCH": {"last_sweep_ts": now - stale,
                      "last_sweep_age_s": stale, "healthy": False},
            "ADSB": {"last_sweep_ts": now - stale,
                     "last_sweep_age_s": stale, "healthy": False}}))
        out = web.observers(self.db, REG, now=now)[0]
        self.assertTrue(out["online"])
        self.assertFalse(out["collecting"])
        self.assertEqual(out["receivers_healthy"], 0)

    def test_ages_are_advanced_by_heartbeat_age(self):
        """A stale heartbeat carries stale ages; they must not read fresh.

        Without this the station reports a 30-second-old sweep forever,
        which is precisely the blind spot the field exists to close.
        """
        now = time.time()
        hb = now - 2 * 86400
        self._put(v2(ts=hb, receivers={
            "BENCH": {"last_sweep_ts": hb - 30, "last_sweep_age_s": 30,
                      "healthy": True}}))
        out = web.observers(self.db, REG, now=now)[0]
        bench = out["receivers"]["BENCH"]
        self.assertGreater(bench["last_sweep_age_s"], 86400)
        self.assertFalse(bench["healthy"])
        self.assertFalse(out["collecting"])

    def test_station_healthy_claim_is_not_trusted(self):
        """The station said healthy at publish time; we recompute for now."""
        now = time.time()
        self._put(v2(ts=now, stale_after_s=10, receivers={
            "BENCH": {"last_sweep_ts": now - 600, "last_sweep_age_s": 600,
                      "healthy": True}}))
        out = web.observers(self.db, REG, now=now)[0]
        self.assertFalse(out["receivers"]["BENCH"]["healthy"])
        self.assertFalse(out["collecting"])

    def test_partial_outage_still_collecting(self):
        now = time.time()
        self._put(v2(ts=now, receivers={
            "BENCH": {"last_sweep_ts": now - 10, "last_sweep_age_s": 10},
            "ADSB": {"last_sweep_ts": now - 99999,
                     "last_sweep_age_s": 99999}}))
        out = web.observers(self.db, REG, now=now)[0]
        self.assertTrue(out["collecting"])
        self.assertEqual(out["receivers_healthy"], 1)
        self.assertEqual(out["receivers_total"], 2)

    def test_monitor_only_receiver_counts_as_alive(self):
        now = time.time()
        self._put(v2(ts=now, receivers={
            "SONDE": {"last_check_ts": now - 15, "last_check_age_s": 15}}))
        out = web.observers(self.db, REG, now=now)[0]
        self.assertTrue(out["collecting"])

    def test_receiver_with_no_timestamps_is_unhealthy(self):
        now = time.time()
        self._put(v2(ts=now, receivers={"BENCH": {}}))
        out = web.observers(self.db, REG, now=now)[0]
        self.assertFalse(out["receivers"]["BENCH"]["healthy"])
        self.assertFalse(out["collecting"])

    def test_malformed_receiver_block_does_not_crash(self):
        now = time.time()
        self._put(v2(ts=now, receivers={"BENCH": "not-a-dict", "ADSB": None}))
        out = web.observers(self.db, REG, now=now)[0]
        self.assertEqual(out["receivers"], {})
        self.assertEqual(out["receivers_total"], 0)

    def test_receivers_not_a_dict_reports_unknown(self):
        now = time.time()
        self._put(v2(ts=now, receivers=["BENCH"]))
        out = web.observers(self.db, REG, now=now)[0]
        self.assertIsNone(out["collecting"])

    def test_health_creates_no_evidence(self):
        """v2 adds liveness only -- never an air claim."""
        now = time.time()
        self._put(v2(ts=now, receivers={
            "BENCH": {"last_sweep_ts": now - 5, "last_sweep_age_s": 5}}))
        self.assertIsNone(web.last_observation_ts(self.db))
        self.assertEqual(web.active(self.db), [])
        self.assertEqual(web.stations(self.db, REG)[0]["observations"], 0)


class StatusSourceTest(unittest.TestCase):
    """The source label must describe what actually fed the response."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DB(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_empty_reports_wildcard_not_a_version(self):
        self.assertEqual(web.status_source(self.db),
                         "rfsurvey.status.* heartbeat (UNSIGNED)")

    def test_v1_only(self):
        ingest.handle_status(status(), REG, self.db, topic_station_id=SID)
        self.assertEqual(web.status_source(self.db),
                         "rfsurvey.status.v1 heartbeat (UNSIGNED)")

    def test_v2_only_does_not_claim_v1(self):
        """The regression: label read v1 while serving v2 data."""
        ingest.handle_status(v2(), REG, self.db, topic_station_id=SID)
        s = web.status_source(self.db)
        self.assertIn("rfsurvey.status.v2", s)
        self.assertNotIn("v1", s)

    def test_mixed_fleet_lists_both(self):
        """A rollout is mixed for its whole length; pick neither side."""
        sid2 = "obs-muehlmini"
        reg = {SID: "k", sid2: "k"}
        ingest.handle_status(status(), reg, self.db, topic_station_id=SID)
        ingest.handle_status(status(sid=sid2, schema="rfsurvey.status.v2"),
                             reg, self.db, topic_station_id=sid2)
        s = web.status_source(self.db)
        self.assertIn("rfsurvey.status.v1", s)
        self.assertIn("rfsurvey.status.v2", s)

    def test_unsigned_marker_survives_every_version(self):
        """Schema version changes; the trust level of this input does not."""
        ingest.handle_status(v2(), REG, self.db, topic_station_id=SID)
        self.assertIn("UNSIGNED", web.status_source(self.db))


if __name__ == "__main__":
    unittest.main()
