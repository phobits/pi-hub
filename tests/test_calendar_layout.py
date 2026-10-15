from __future__ import annotations

import unittest

from smart_display.calendar_layout import compute_row_budget


class ComputeRowBudgetTest(unittest.TestCase):
    """Plan B7: ``compute_row_budget`` is the Python source-of-truth for
    the row distribution logic the calendar list uses. The JS mirror in
    ``app.js`` (``computeRowBudget``) must agree with these cases."""

    def test_empty_sections_yields_empty_list(self) -> None:
        self.assertEqual(
            compute_row_budget([], 10, section_has_label=[]),
            [],
        )

    def test_zero_budget_allocates_nothing(self) -> None:
        self.assertEqual(
            compute_row_budget([3, 2], 0, section_has_label=[False, True]),
            [0, 0],
        )

    def test_single_section_fits(self) -> None:
        self.assertEqual(
            compute_row_budget([5], 10, section_has_label=[False]),
            [5],
        )

    def test_multiple_sections_all_fit(self) -> None:
        # 3 items + (1 label + 2 items) + (1 label + 1 item) = 8 rows, budget 10.
        result = compute_row_budget(
            [3, 2, 1], 10, section_has_label=[False, True, True]
        )
        self.assertEqual(result, [3, 2, 1])

    def test_largest_section_shrinks_first(self) -> None:
        # 5 + (1 + 3) + (1 + 2) = 12 rows needed, budget 9 → shave the
        # largest until it fits.
        result = compute_row_budget(
            [5, 3, 2], 9, section_has_label=[False, True, True]
        )
        # 12 → 11 (5-1=4) → 11 > 9 → 10 (4-1=3, now ties with section 1)
        # → 9 (shave 3 → 2)
        # Final shape: each section keeps at least some items, later days
        # stay visible.
        self.assertEqual(sum(result) + sum(
            1 for i, had in enumerate([False, True, True]) if had and result[i] > 0
        ), 9)
        self.assertTrue(all(x > 0 for x in result))

    def test_section_can_be_trimmed_to_zero_and_drops_label(self) -> None:
        # The old fixture ([20,5,3] over 5 rows) never actually trimmed anything
        # to zero — it returned [1,1,1] and the assertions were loose enough not
        # to notice, so this path was uncovered. A budget too small for one row
        # plus one label per section does force sections out.
        result = compute_row_budget(
            [6, 2, 1], 3, section_has_label=[True, True, True]
        )
        self.assertEqual(result, [1, 0, 0])
        # Zeroed sections must stop costing a label row, or the result would not
        # fit the budget it was computed against.
        label_rows = sum(1 for count in result if count > 0)
        self.assertLessEqual(sum(result) + label_rows, 3)
        # Today survives — it is the last thing the trim gives up.
        self.assertGreaterEqual(result[0], 1)

    def test_no_later_section_is_dropped_before_trimming_largest(self) -> None:
        # today=3, tomorrow=2 (+label), day3=1 (+label) = 8 rows, budget 7.
        # The largest section (tied today vs. tomorrow at 3 rows each) loses
        # one item; both other sections stay visible.
        result = compute_row_budget(
            [3, 2, 1], 7, section_has_label=[False, True, True]
        )
        self.assertEqual(sum(result), 5)
        self.assertTrue(all(x > 0 for x in result))
        # Budget check: total allocated rows + labels for non-empty sections
        # must fit.
        total = sum(result) + sum(
            1 for i, had in enumerate([False, True, True]) if had and result[i] > 0
        )
        self.assertLessEqual(total, 7)

    def test_tie_trims_the_latest_section_not_today(self) -> None:
        # One appointment per day is the common case, so every section ties.
        # Sections are chronological: trimming the first would drop *today* and
        # keep the day after tomorrow, which is backwards.
        result = compute_row_budget(
            [1, 1, 1], 4, section_has_label=[True, True, True]
        )
        self.assertEqual(result, [1, 1, 0])

    def test_tie_keeps_trimming_from_the_back(self) -> None:
        result = compute_row_budget(
            [1, 1, 1], 2, section_has_label=[True, True, True]
        )
        self.assertEqual(result, [1, 0, 0])

    def test_extra_rows_count_against_the_budget(self) -> None:
        # Homescreen v2: the highlighted next event is one row taller, so its
        # section carries one extra row while it shows anything. Without the
        # extra, [1, 1] costs 4 rows (two labels, two items) and fits a budget
        # of 4; with today's extra it costs 5, so the trim runs once more and
        # — ties go to the latest section — tomorrow drops out.
        self.assertEqual(
            compute_row_budget([2, 1], 4, section_has_label=[True, True]),
            [1, 1],
        )
        self.assertEqual(
            compute_row_budget(
                [2, 1],
                4,
                section_has_label=[True, True],
                section_extra_rows=[1, 0],
            ),
            [1, 0],
        )

    def test_extra_rows_length_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            compute_row_budget(
                [1, 1], 4, section_has_label=[True, True], section_extra_rows=[1]
            )

    def test_label_length_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            compute_row_budget([1, 2], 5, section_has_label=[True])

    def test_negative_counts_clamp_to_zero(self) -> None:
        self.assertEqual(
            compute_row_budget([-1, 2], 5, section_has_label=[False, True]),
            [0, 2],
        )


if __name__ == "__main__":
    unittest.main()
