import base64
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ssrfobs import ingest, web
from ssrfobs.db import DB

from test_ingest import batch, make_station, sign


class TestWeb(unittest.TestCase):
    def setUp(self):
        self.key, self.pub = make_station()
        self.reg = {"sta-a": self.pub}
        self.db = DB(os.path.join(tempfile.mkdtemp(), "obs.db"))

    def ingest_one(self, bid="b-1", cc=9, sid="sta-a", key=None):
        env = sign(key or self.key, batch(sid, bid=bid, cc=cc), sid)
        ingest.handle(json.dumps(env).encode(), self.reg, self.db)

    def test_empty_surface_renders(self):
        page = web.render(self.db, self.reg)
        self.assertIn("RF OBSERVERS", page)
        self.assertIn("no observations yet", page)
        self.assertIn("no graded channels yet", page)
        self.assertEqual(web.feed(self.db), [])
        self.assertEqual(web.channels(self.db), {})

    def test_feed_and_channels_after_ingest(self):
        self.ingest_one()
        items = web.feed(self.db)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["station_id"], "sta-a")
        self.assertEqual(items[0]["freq_mhz"], 460.0)
        self.assertEqual(items[0]["level"], "V1")
        self.assertEqual(items[0]["decoder"], "dmr")

        ch = web.channels(self.db)
        self.assertIn(460000000, ch)
        self.assertEqual(ch[460000000]["level"], "V1")

        page = web.render(self.db, self.reg)
        self.assertIn("460.0000 MHz", page)
        self.assertIn("sta-a", page)

    def test_feed_limit_and_freq_filter(self):
        for i in range(5):
            self.ingest_one(bid="b-%d" % i)
        self.assertEqual(len(web.feed(self.db, limit=2)), 2)
        self.assertEqual(len(web.feed(self.db, freq_hz=460000000)), 5)
        self.assertEqual(web.feed(self.db, freq_hz=123000000), [])
        # hard cap holds even if a caller asks for more
        self.assertLessEqual(len(web.feed(self.db, limit=99999)),
                             web.MAX_LIMIT)

    def test_stations_never_leak_pubkeys(self):
        self.ingest_one()
        sts = web.stations(self.db, self.reg)
        self.assertEqual(len(sts), 1)
        self.assertEqual(sts[0]["station_id"], "sta-a")
        self.assertEqual(sts[0]["observations"], 1)
        self.assertEqual(sts[0]["sweep_bins"], 1)
        blob = json.dumps(sts) + web.render(self.db, self.reg)
        self.assertNotIn(self.pub, blob)

    def test_flagged_status_shows_on_page(self):
        keyb, pubb = make_station()
        self.reg["sta-b"] = pubb
        self.ingest_one(bid="b-a", cc=9)
        self.ingest_one(bid="b-b", cc=5, sid="sta-b", key=keyb)
        page = web.render(self.db, self.reg)
        self.assertIn("FLAGGED", page)


if __name__ == "__main__":
    unittest.main()
