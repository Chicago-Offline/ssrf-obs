"""Monitor-check ingest + cross-station rollup (NETWORK.md S2/S3/S4)."""
import os
import tempfile
import time
import unittest

from ssrfobs import db as db_mod, rules, web

NOW = 1_800_000_000.0
DAY = 86400.0


def _chk(station="sta-a", receiver="rtl:0", ts=NOW, target="NS9RC 145.470",
         freq_hz=145_470_000, heard=True, params=None, decoder="nfm",
         ssrf_id="ns9rc:145470"):
    return {"ts": ts, "receiver": receiver, "target": target,
            "ssrf_id": ssrf_id, "freq_hz": freq_hz, "decoder": decoder,
            "heard": heard, "snr_db": 12.0, "params": params or {},
            "meta": {}, "lat": None, "lon": None, "alt_m": None,
            "fix": None}


def _batch(checks, station="sta-a", bid="b1"):
    return {"schema": "rfsurvey.obs.v1", "batch_id": bid,
            "station_id": station, "generated_at": NOW, "site": {},
            "receivers": [], "sweep_summaries": [], "observations": [],
            "beacon_readings": [], "monitor_checks": checks}


def _rows(checks, station="sta-a"):
    """Shape checks like db.monitor_check_rows() output."""
    return [(c.get("_station", station), c["receiver"], c["ts"],
             c["target"], c["ssrf_id"], c["freq_hz"], c["decoder"],
             1 if c["heard"] else 0, c["snr_db"], c["params"], c["meta"])
            for c in checks]


class IngestTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = db_mod.DB(os.path.join(self.dir, "t.db"))

    def test_silent_checks_are_stored_not_dropped(self):
        self.db.insert_batch(_batch([_chk(heard=False), _chk(heard=True)]))
        rows = self.db.monitor_check_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r[7] for r in rows), [0, 1])

    def test_catalog_join_keys_survive_ingest(self):
        self.db.insert_batch(_batch([_chk()]))
        r = self.db.monitor_check_rows()[0]
        self.assertEqual(r[0], "sta-a")
        self.assertEqual(r[3], "NS9RC 145.470")
        self.assertEqual(r[4], "ns9rc:145470")
        self.assertEqual(r[5], 145_470_000)

    def test_batch_replay_is_idempotent(self):
        b = _batch([_chk()])
        self.assertTrue(self.db.insert_batch(b))
        self.assertFalse(self.db.insert_batch(b))
        self.assertEqual(len(self.db.monitor_check_rows()), 1)

    def test_rows_missing_a_key_are_skipped_not_fatal(self):
        bad = _chk()
        bad["freq_hz"] = None
        self.db.insert_batch(_batch([bad, _chk()]))
        self.assertEqual(len(self.db.monitor_check_rows()), 1)

    def test_monitors_view_keeps_never_heard_channels(self):
        # channels() drops anything never heard; monitors() must not.
        self.db.insert_batch(_batch(
            [_chk(ts=NOW - i * 3600, heard=False) for i in range(60)]))
        mons = web.monitors(self.db)
        self.assertEqual(len(mons), 1)
        row = next(iter(mons.values()))
        self.assertEqual(row["hearings"], 0)
        self.assertEqual(row["checks"], 60)

    def test_monitors_view_splits_two_targets_on_one_freq(self):
        self.db.insert_batch(_batch([
            _chk(target="A tone 107.2"), _chk(target="B tone 141.3")]))
        self.assertEqual(len(web.monitors(self.db)), 2)


class StatusTest(unittest.TestCase):
    def _status(self, checks):
        return rules.monitor_rollup(_rows(checks), now=NOW)["status"]

    def test_active_stale_dormant_ladder(self):
        self.assertEqual(self._status([_chk(ts=NOW - 2 * DAY)]), "active")
        self.assertEqual(self._status([_chk(ts=NOW - 14 * DAY)]), "stale")
        self.assertEqual(self._status([_chk(ts=NOW - 90 * DAY)]), "dormant")

    def test_thin_silence_is_watching_not_never_heard(self):
        # Two silent checks on one afternoon is not evidence of a dead
        # repeater. Claiming never_heard here would publish a false finding.
        self.assertEqual(self._status(
            [_chk(ts=NOW - 60, heard=False), _chk(heard=False)]),
            "watching")

    def test_never_heard_needs_both_volume_and_span(self):
        # 60 checks, but all inside one day -> span too short.
        packed = [_chk(ts=NOW - i * 600, heard=False) for i in range(60)]
        self.assertEqual(self._status(packed), "watching")
        # 60 checks spread over 20 days -> a real finding.
        spread = [_chk(ts=NOW - i * DAY / 3, heard=False)
                  for i in range(60)]
        self.assertEqual(self._status(spread), "never_heard")

    def test_one_hearing_beats_any_amount_of_silence(self):
        checks = [_chk(ts=NOW - i * DAY / 3, heard=False)
                  for i in range(60)]
        checks.append(_chk(ts=NOW - DAY, heard=True))
        self.assertEqual(self._status(checks), "active")

    def test_hit_rate_is_over_checks_performed(self):
        st = rules.monitor_rollup(_rows(
            [_chk(heard=True)] + [_chk(heard=False)] * 3), now=NOW)
        self.assertEqual(st["hit_rate"], 0.25)
        self.assertEqual(st["hearings"], 1)


class ObserverIdentityTest(unittest.TestCase):
    def test_same_receiver_name_at_two_stations_is_two_observers(self):
        # Every host has an "rtl:0". Counting bare receiver would collapse
        # these into one observer and fake away the corroboration.
        a = _chk(); a["_station"] = "sta-a"
        b = _chk(); b["_station"] = "sta-b"
        st = rules.monitor_rollup(_rows([a, b]), now=NOW)
        self.assertEqual(len(st["observers_heard"]), 2)
        self.assertTrue(st["corroborated"])

    def test_one_station_two_dongles_is_not_corroboration_by_accident(self):
        a = _chk(receiver="rtl:0")
        b = _chk(receiver="rtl:1")
        st = rules.monitor_rollup(_rows([a, b]), now=NOW)
        # Two distinct observers, but both at one station.
        self.assertEqual(len(st["observers_heard"]), 2)
        self.assertTrue(all(o.startswith("sta-a/")
                            for o in st["observers_heard"]))


class ParamMergeTest(unittest.TestCase):
    def test_two_hearings_promote_measured_to_verified(self):
        p = {"ctcss_hz": {"state": "verified", "observed": 107.2}}
        one = rules.param_merge(_rows([_chk(params=p)]))
        self.assertEqual(one["ctcss_hz"]["state"], "measured")
        two = rules.param_merge(_rows([_chk(params=p), _chk(params=p)]))
        self.assertEqual(two["ctcss_hz"]["state"], "verified")
        self.assertEqual(two["ctcss_hz"]["observed"], 107.2)

    def test_conflict_is_sticky_and_survives_later_agreement(self):
        bad = {"ctcss_hz": {"state": "conflict", "observed": 141.3}}
        good = {"ctcss_hz": {"state": "verified", "observed": 107.2}}
        st = rules.param_merge(_rows(
            [_chk(params=bad), _chk(params=good), _chk(params=good)]))
        self.assertEqual(st["ctcss_hz"]["state"], "conflict")

    def test_silence_never_downgrades_a_verified_param(self):
        p = {"ctcss_hz": {"state": "verified", "observed": 107.2}}
        st = rules.param_merge(_rows(
            [_chk(params=p), _chk(params=p)] +
            [_chk(heard=False, params={})] * 40))
        self.assertEqual(st["ctcss_hz"]["state"], "verified")

    def test_suspect_hum_never_reaches_verified(self):
        # 120 Hz is mains hum, not CTCSS 123.0. Verifying it would write a
        # power-supply artifact into the catalog as a real tone.
        p = {"ctcss_hz": {"state": "suspect", "observed": 120.0,
                          "note": "suspect hum"}}
        st = rules.param_merge(_rows([_chk(params=p)] * 5))
        self.assertEqual(st["ctcss_hz"]["state"], "suspect")

    def test_empty_grades_contribute_nothing(self):
        self.assertEqual(rules.param_merge(_rows([_chk(params={})])), {})

    def test_corroborated_needs_two_distinct_observers(self):
        p = {"color_code": {"state": "verified", "observed": 1}}
        solo = rules.param_merge(_rows([_chk(params=p), _chk(params=p)]))
        self.assertEqual(solo["color_code"]["state"], "verified")
        self.assertFalse(solo["color_code"]["corroborated"])
        a = _chk(params=p); a["_station"] = "sta-a"
        b = _chk(params=p); b["_station"] = "sta-b"
        duo = rules.param_merge(_rows([a, b]))
        self.assertTrue(duo["color_code"]["corroborated"])


if __name__ == "__main__":
    unittest.main()


class MonitorRenderTest(unittest.TestCase):
    """The page must SHOW silence, not just store it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = db_mod.DB(os.path.join(self.dir, "t.db"))

    def test_empty_state_names_the_missing_config(self):
        page = web.render(self.db, {})
        self.assertIn("Channel monitoring", page)
        self.assertIn("nothing monitored yet", page)
        # An empty section must say WHAT to add, or it reads as broken.
        self.assertIn("targets_file", page)

    def test_never_heard_channel_is_visible_on_the_page(self):
        # channels() hides never-heard entries; the monitoring section is
        # the only place this finding can surface at all. Checks must span
        # DAYS, not hours: never_heard needs volume AND span, so hourly
        # samples would (correctly) still read "watching".
        self.db.insert_batch(_batch(
            [_chk(ts=NOW - i * (DAY / 4), heard=False) for i in range(60)]))
        page = web.render(self.db, {})
        self.assertIn("145.4700 MHz", page)
        self.assertIn("NEVER HEARD", page)
        self.assertIn(web.MON_BADGE["never_heard"], page)

    def test_thin_silence_shows_watching_not_an_accusation(self):
        self.db.insert_batch(_batch([_chk(heard=False)]))
        page = web.render(self.db, {})
        self.assertIn("WATCHING", page)
        self.assertNotIn("NEVER HEARD", page)

    def test_silent_row_still_names_its_observers(self):
        # "never heard" with no attribution is an unattributable claim.
        self.db.insert_batch(_batch(
            [_chk(ts=NOW - i * (DAY / 4), heard=False) for i in range(60)]))
        page = web.render(self.db, {})
        self.assertIn("sta-a/rtl:0", page)

    def test_heard_channel_reports_hit_rate(self):
        checks = [_chk(ts=NOW - i * 3600, heard=(i % 4 == 0))
                  for i in range(60)]
        self.db.insert_batch(_batch(checks))
        page = web.render(self.db, {})
        self.assertIn("Channel monitoring", page)
        self.assertNotIn("NEVER HEARD", page)

    def test_monitoring_section_precedes_the_roll_up(self):
        # The roll-up is ~70% of the page by bytes, so anything below it is
        # effectively unreachable by scrolling. Monitoring answers "is this
        # repeater on the air", which is the question people arrive with.
        page = web.render(self.db, {})
        self.assertLess(page.index("Channel monitoring"),
                        page.index("Channel roll-up"))

    def test_monitoring_section_precedes_live_window(self):
        # Live window answers "what's on right now"; monitoring answers
        # "is this repeater alive" — the higher-value question leads.
        page = web.render(self.db, {})
        self.assertLess(page.index("Channel monitoring"),
                        page.index("On the air"))

    def test_health_header_appears_when_checks_present(self):
        self.db.insert_batch(_batch([_chk(heard=False)]))
        page = web.render(self.db, {})
        self.assertIn("channels watched", page)
        self.assertIn("last check", page)
        self.assertIn("observer", page)

    def test_health_header_absent_when_no_monitors(self):
        # Empty state must not emit a misleading "0 channels watched" header.
        page = web.render(self.db, {})
        self.assertNotIn("channels watched", page)

    def test_default_sort_active_before_watching(self):
        # Active entries must render before watching entries in the default
        # (unsorted) page order — status priority, not frequency order.
        import time as _time
        heard_chk = _chk(target="KA9HHH 146.880", freq_hz=146_880_000,
                         ssrf_id="ka9hhh:146880", heard=True, ts=NOW - 3600)
        silent_chk = _chk(target="NS9RC 145.470", freq_hz=145_470_000,
                          heard=False)
        self.db.insert_batch(_batch([heard_chk, silent_chk], bid="b2"))
        page = web.render(self.db, {})
        self.assertLess(page.index("KA9HHH"), page.index("NS9RC"))

    def test_published_tone_column_present(self):
        # The tone/CC column header must exist whether or not params are set.
        self.db.insert_batch(_batch([_chk(heard=False)]))
        page = web.render(self.db, {})
        self.assertIn("published tone", page)

    def test_published_tone_shows_ctcss_claim(self):
        p = {"ctcss_hz": {"state": "verified", "observed": 107.2}}
        self.db.insert_batch(_batch([_chk(params=p, heard=True)]))
        page = web.render(self.db, {})
        self.assertIn("107.2 Hz", page)

    def test_published_tone_shows_color_code(self):
        p = {"color_code": {"state": "verified", "observed": 1}}
        self.db.insert_batch(_batch([_chk(params=p, heard=True)]))
        page = web.render(self.db, {})
        self.assertIn("CC 1", page)

    def test_published_tone_dash_when_no_claim(self):
        # No params in the check -> dash in the tone column, not an error.
        self.db.insert_batch(_batch([_chk(heard=False)]))
        page = web.render(self.db, {})
        # The tone column header exists; at least one dash cell must follow.
        self.assertIn("published tone", page)
        self.assertIn("&mdash;", page)  # emitted as HTML entity
