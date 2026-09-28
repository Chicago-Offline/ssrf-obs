import base64
import gzip
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from ssrfobs import ingest, rules, verify
from ssrfobs.db import DB


def make_station():
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, base64.b64encode(raw).decode()


def sign(key, batch, sid):
    sig = key.sign(verify.canonical(batch))
    return {"batch": batch, "sig": base64.b64encode(sig).decode(),
            "station_id": sid}


def batch(sid, bid="b-1", ts=None, cc=9):
    return {"schema": "rfsurvey.obs.v1", "batch_id": bid, "station_id": sid,
            "generated_at": time.time(), "site": {},
            "sweep_summaries": [{"receiver": "R", "freq_hz": 460000000,
                                 "median_db": -55.0, "max_db": -38.0, "hits": 12}],
            "observations": [{"id": 1, "ts": ts or time.time(), "receiver": "R",
                              "freq_hz": 460000000, "snr_db": 18.0,
                              "duration_s": 90, "decoder": "dmr", "gated": True,
                              "meta": {"cc": cc, "tgs": [100]},
                              "lat": 41.97, "lon": -87.69, "alt_m": 180,
                              "fix": "static"}]}


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.key, self.pub = make_station()
        self.reg = {"sta-a": self.pub}
        self.db = DB(os.path.join(tempfile.mkdtemp(), "obs.db"))

    def test_accept_dedupe_and_gzip(self):
        env = sign(self.key, batch("sta-a"), "sta-a")
        raw = json.dumps(env).encode()
        self.assertEqual(ingest.handle(gzip.compress(raw), self.reg, self.db), "b-1")
        ingest.handle(raw, self.reg, self.db)  # replay: no-op
        n = self.db.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        self.assertEqual(n, 1)

    def test_reject_unknown_tampered_spoofed(self):
        other_key, _ = make_station()
        with self.assertRaises(ingest.Reject):   # unknown station
            ingest.handle(json.dumps(sign(self.key, batch("sta-x"), "sta-x")).encode(),
                          self.reg, self.db)
        env = sign(self.key, batch("sta-a"), "sta-a")
        env["batch"]["observations"][0]["snr_db"] = 99.0   # tamper
        with self.assertRaises(ingest.Reject):
            ingest.handle(json.dumps(env).encode(), self.reg, self.db)
        env2 = sign(other_key, batch("sta-a"), "sta-a")    # wrong key
        with self.assertRaises(ingest.Reject):
            ingest.handle(json.dumps(env2).encode(), self.reg, self.db)

    def test_promotion_rules(self):
        keyb, pubb = make_station()
        self.reg["sta-b"] = pubb
        now = time.time()
        # V1 decodes from 2 stations across 3 days -> verified
        for i, (sid, k) in enumerate([("sta-a", self.key), ("sta-b", keyb),
                                      ("sta-a", self.key)]):
            ingest.handle(json.dumps(sign(
                k, batch(sid, bid=f"b-{i+10}", ts=now - i * 86400), sid)).encode(),
                self.reg, self.db)
        st = rules.channel_status(self.db.observation_rows(460000000), now=now)
        self.assertEqual(st["status"], "verified")
        self.assertEqual(st["level"], "V1")
        # conflicting color code -> flagged
        ingest.handle(json.dumps(sign(
            keyb, batch("sta-b", bid="b-cc", cc=5), "sta-b")).encode(),
            self.reg, self.db)
        st = rules.channel_status(self.db.observation_rows(460000000), now=now)
        self.assertEqual(st["status"], "flagged")

    def test_tiers(self):
        self.assertEqual(rules.tier(True, "dmr", {"cc": 9}), 1)
        self.assertEqual(rules.tier(True, "nfm", {"voice": True}), 2)
        self.assertEqual(rules.tier(True, "dmr", {"recording": "x.wav"}), 3)
        self.assertEqual(rules.tier(True, "nfm", {}), 0)
        self.assertIsNone(rules.tier(False, "dmr", {"cc": 9}))  # decoders lie on noise


if __name__ == "__main__":
    unittest.main()
