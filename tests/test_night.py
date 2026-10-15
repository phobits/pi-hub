from __future__ import annotations

import unittest
from datetime import datetime

from smart_display.night import is_night, is_night_minutes, parse_hhmm, window_for_ui


class ParseTest(unittest.TestCase):
    def test_parse_hhmm(self) -> None:
        self.assertEqual(parse_hhmm("22:30"), 1350)
        self.assertEqual(parse_hhmm("00:00"), 0)
        self.assertEqual(parse_hhmm(" 6:05 "), 365)
        for bad in ("", None, "24:00", "12:60", "abc", "12", "1:2:3"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_hhmm(bad))


class WindowTest(unittest.TestCase):
    def test_wrapping_window(self) -> None:
        start, end = parse_hhmm("22:30"), parse_hhmm("06:00")
        self.assertTrue(is_night_minutes(parse_hhmm("22:30"), start, end))
        self.assertTrue(is_night_minutes(parse_hhmm("23:59"), start, end))
        self.assertTrue(is_night_minutes(parse_hhmm("00:00"), start, end))
        self.assertTrue(is_night_minutes(parse_hhmm("05:59"), start, end))
        self.assertFalse(is_night_minutes(parse_hhmm("06:00"), start, end))
        self.assertFalse(is_night_minutes(parse_hhmm("12:00"), start, end))
        self.assertFalse(is_night_minutes(parse_hhmm("22:29"), start, end))

    def test_same_day_window(self) -> None:
        start, end = parse_hhmm("13:00"), parse_hhmm("14:00")
        self.assertTrue(is_night_minutes(parse_hhmm("13:30"), start, end))
        self.assertFalse(is_night_minutes(parse_hhmm("14:00"), start, end))
        self.assertFalse(is_night_minutes(parse_hhmm("12:59"), start, end))

    def test_disabled_windows(self) -> None:
        self.assertFalse(is_night_minutes(100, None, 200))
        self.assertFalse(is_night_minutes(100, 100, 100))
        self.assertFalse(is_night(datetime(2026, 1, 1, 23, 0), "", "06:00"))
        self.assertIsNone(window_for_ui("", "06:00"))
        self.assertIsNone(window_for_ui("22:00", "22:00"))

    def test_window_for_ui(self) -> None:
        self.assertEqual(window_for_ui("22:30", "06:00"), {"from": "22:30", "to": "06:00"})

    def test_is_night_uses_wall_clock(self) -> None:
        self.assertTrue(is_night(datetime(2026, 1, 1, 23, 15), "22:30", "06:00"))
        self.assertFalse(is_night(datetime(2026, 1, 1, 9, 0), "22:30", "06:00"))


if __name__ == "__main__":
    unittest.main()
