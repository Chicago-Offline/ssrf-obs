"""Monitoring-health rollup: the line that replaced per-row last-checked."""

import time
import unittest

from ssrfobs import ui


def _m(age_s):
    return {"last_checked": time.time() - age_s}


class WindowTextTest(unittest.TestCase):
    def test_renders_the_threshold_actually_applied(self):
        # The page must never advertise a window it does not enforce.
        self.assertEqual(ui.window_text(3600), "1 h")
        self.assertEqual(ui.window_text(7200), "2 h")
        self.assertEqual(ui.window_text(300), "5 min")
        self.assertEqual(ui.window_text(1800), "30 min")

    def test_label_matches_the_constant(self):
        _g, _c, text = ui.health({"a": _m(10)})
        self.assertIn(ui.window_text(ui.HEALTHY_CHECK_S), text)


class HealthTest(unittest.TestCase):
    def test_threshold_is_tuned_to_real_cadence_not_five_minutes(self):
        # Measured on prod 2026-09-28: 57 targets, min age 6.3 min, p50
        # 12.6, p90 51.8. A 5-minute bar reads 0/57 forever, so it would
        # be noise rather than a health signal. Guard the regression.
        self.assertGreater(ui.HEALTHY_CHECK_S, 600)
        typical = {"m%d" % i: _m(mins * 60)
                   for i, mins in enumerate((6.3, 12.6, 12.6, 51.8))}
        glyph, _c, text = ui.health(typical)
        self.assertEqual(glyph, "\u25cf")
        self.assertIn("Monitoring healthy", text)
        self.assertIn("4/4", text)

    def test_all_fresh_reads_healthy(self):
        glyph, _c, text = ui.health({"a": _m(60), "b": _m(120)})
        self.assertEqual(glyph, "\u25cf")
        self.assertIn("Monitoring healthy \u00b7 2/2", text)

    def test_a_lagging_target_degrades_the_line(self):
        glyph, _c, text = ui.health({"a": _m(60),
                                     "b": _m(6 * 3600)})
        self.assertEqual(glyph, "\u26a0")
        self.assertIn("Monitoring degraded \u00b7 1/2", text)

    def test_never_checked_counts_against_health(self):
        glyph, _c, text = ui.health({"a": {"last_checked": None}})
        self.assertEqual(glyph, "\u26a0")
        self.assertIn("0/1", text)

    def test_no_monitors_is_not_reported_as_healthy(self):
        glyph, _c, text = ui.health({})
        self.assertEqual(glyph, "\u26a0")
        self.assertIn("No monitored repeaters", text)


class StaleNoteTest(unittest.TestCase):
    def test_silent_while_monitoring_is_keeping_up(self):
        # Eric: last-checked appears per-repeater ONLY when wrong.
        self.assertIsNone(ui.stale_note(time.time() - 60))

    def test_reports_hours_like_the_brief(self):
        note = ui.stale_note(time.time() - 3 * 3600)
        self.assertEqual(note,
                         "\u26a0 Monitoring stale \u2014 last checked 3h ago")

    def test_reports_days_once_it_is_that_bad(self):
        self.assertIn("2d ago", ui.stale_note(time.time() - 2 * 86400))

    def test_never_checked_is_called_out(self):
        self.assertIn("never checked", ui.stale_note(None))


if __name__ == "__main__":
    unittest.main()
