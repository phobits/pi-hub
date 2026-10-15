from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from smart_display.i18n import GERMAN_MONTHS_LONG, format_initial_clock


class FormatInitialClockTest(unittest.TestCase):
    """Plan B14: the hero clock must render useful German labels on the
    server so a cold reload never shows a ``--:--`` placeholder."""

    def test_month_list_is_complete(self) -> None:
        self.assertEqual(len(GERMAN_MONTHS_LONG), 12)
        self.assertEqual(GERMAN_MONTHS_LONG[0], "Januar")
        self.assertEqual(GERMAN_MONTHS_LONG[2], "März")

    def test_formats_initial_clock_with_german_weekday(self) -> None:
        # 2026-04-08 09:42 UTC → 11:42 in Vienna (summer DST).
        frozen = datetime(2026, 4, 8, 9, 42, tzinfo=ZoneInfo("UTC"))
        result = format_initial_clock("Europe/Vienna", now=frozen)
        self.assertEqual(result["time"], "11:42")
        self.assertEqual(result["date"], "Mittwoch, 8. April")
        self.assertNotIn("timezone_label", result)

    def test_formats_initial_clock_respects_zone_difference(self) -> None:
        # Same UTC instant, different zone → different HH:MM.
        frozen = datetime(2026, 4, 8, 23, 30, tzinfo=ZoneInfo("UTC"))
        result = format_initial_clock("UTC", now=frozen)
        self.assertEqual(result["time"], "23:30")
        self.assertEqual(result["date"], "Mittwoch, 8. April")

    def test_all_months_round_trip(self) -> None:
        # Sanity: each month index yields the expected German name.
        for month_index, name in enumerate(GERMAN_MONTHS_LONG, start=1):
            frozen = datetime(2026, month_index, 1, 12, 0, tzinfo=ZoneInfo("UTC"))
            result = format_initial_clock("UTC", now=frozen)
            self.assertIn(name, result["date"])


if __name__ == "__main__":
    unittest.main()
