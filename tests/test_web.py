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
        # Activity is the default screen now, so the observer roster moved
        # to /network. Each fact is asserted against the page that owns it.
        page = web.render(self.db, self.reg)
        self.assertIn("Chicago Repeaters", page)
        self.assertIn("no amateur or GMRS repeaters heard", page)
        net = web.render_network(self.db, self.reg)
        # An enrolled station renders even before it reports anything.
        self.assertIn("sta-a", net)
        self.assertIn("no beacon reference", net)
        self.assertEqual(web.feed(self.db), [])
        self.assertEqual(web.channels(self.db), {})
        self.assertEqual(web.repeaters(self.db), [])

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
        # Observer attribution lives on the Network tab now.
        self.assertIn("sta-a", web.render_network(self.db, self.reg))
        # Unnamed non-repeater energy stays in the JSON feeds, off the page.
        self.assertNotIn("460.0000 MHz", page)

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

    def test_flagged_status_survives_in_channels_json(self):
        # The page no longer renders the roll-up, but the finding must
        # still be visible to consumers of /channels.json.
        keyb, pubb = make_station()
        self.reg["sta-b"] = pubb
        self.ingest_one(bid="b-a", cc=9)
        self.ingest_one(bid="b-b", cc=5, sid="sta-b", key=keyb)
        ch = web.channels(self.db)
        self.assertEqual(ch[460000000]["status"], "flagged")


if __name__ == "__main__":
    unittest.main()
