"""
Unit tests for commute schedule and lighting logic in server.py
"""

import unittest
from datetime import datetime
from unittest.mock import patch

from server import get_commute_lighting, get_target_poll_interval, is_peak_commute_hours


class TestCommuteSchedule(unittest.TestCase):
    def test_morning_rush(self):
        dt = datetime(2026, 10, 8, 8, 15)
        self.assertTrue(is_peak_commute_hours(dt))
        self.assertEqual(get_commute_lighting(dt), (8, 12))
        self.assertEqual(get_target_poll_interval(dt), 60)

    def test_evening_rush(self):
        dt = datetime(2026, 10, 8, 17, 30)
        self.assertTrue(is_peak_commute_hours(dt))
        self.assertEqual(get_commute_lighting(dt), (8, 12))
        self.assertEqual(get_target_poll_interval(dt), 60)

    def test_midday_eco(self):
        dt = datetime(2026, 10, 8, 13, 30)
        self.assertFalse(is_peak_commute_hours(dt))
        self.assertEqual(get_commute_lighting(dt), (0, 0))
        self.assertEqual(get_target_poll_interval(dt), 600)

    def test_overnight_lighting_off(self):
        # Overnight the frontlight stays off regardless of the poll interval.
        dt = datetime(2026, 10, 8, 2, 0)
        self.assertEqual(get_commute_lighting(dt), (0, 0))

    def test_peak_window_end_is_exclusive(self):
        # Defaults: morning peak ends at 9:30 (exclusive).
        self.assertTrue(is_peak_commute_hours(datetime(2026, 10, 8, 9, 29)))
        self.assertFalse(is_peak_commute_hours(datetime(2026, 10, 8, 9, 30)))

    def test_overnight_deep_eco(self):
        # Default overnight window is 22:00-06:00 -> 3600s.
        cases = [
            (datetime(2026, 10, 8, 23, 0), 3600),
            (datetime(2026, 10, 8, 2, 0), 3600),
            (datetime(2026, 10, 8, 5, 59), 3600),
            # Boundaries: 06:00 is no longer overnight (off-peak 600s).
            (datetime(2026, 10, 8, 6, 0), 600),
            (datetime(2026, 10, 8, 21, 59), 600),
        ]
        for dt, expected in cases:
            with self.subTest(dt=dt, expected=expected):
                self.assertEqual(get_target_poll_interval(dt), expected)

    def test_is_overnight_hours_wraps_midnight(self):
        from server import is_overnight_hours

        cases = [
            (datetime(2026, 10, 8, 23, 30), True),
            (datetime(2026, 10, 8, 0, 30), True),
            (datetime(2026, 10, 8, 12, 0), False),
        ]
        for dt, expected in cases:
            with self.subTest(dt=dt, expected=expected):
                self.assertEqual(is_overnight_hours(dt), expected)

    def test_commute_lighting_named_tuple(self):
        rush = get_commute_lighting(datetime(2026, 10, 8, 8, 15))
        self.assertEqual(rush.brightness, 8)
        self.assertEqual(rush.warmth, 12)
        self.assertEqual(rush[0], 8)
        self.assertEqual(rush[1], 12)
        b, w = rush
        self.assertEqual((b, w), (8, 12))

        off = get_commute_lighting(datetime(2026, 10, 8, 13, 30))
        self.assertEqual(off.brightness, 0)
        self.assertEqual(off.warmth, 0)

    def test_force_fast_poll_overrides_schedule(self):
        import schedule
        from state_machine import ConfigStore

        import server

        forced = ConfigStore(path="", env={"FORCE_FAST_POLL": "1"}, log=lambda *_: None)
        with patch.object(schedule, "STORE", forced):
            # Even at 3 AM, forced-fast returns 60s.
            self.assertEqual(
                server.get_target_poll_interval(datetime(2026, 10, 8, 3, 0)), 60
            )
            # Lighting has never been affected by the testing override.
            self.assertEqual(
                server.get_commute_lighting(datetime(2026, 10, 8, 3, 0)), (0, 0)
            )


if __name__ == "__main__":
    unittest.main()
