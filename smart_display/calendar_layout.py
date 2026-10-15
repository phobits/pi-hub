"""Calendar label helpers.

Pure functions that map a section's ISO date plus the current "today" to a
human label ("Heute"/"Morgen"/"Übermorgen"/"Mittwoch"…). The equivalent JS
implementation lives in ``smart_display/web/static/js/app.js``
(``computeDayLabel``); keep them in sync — Python is the source of truth
for tests.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Sequence

from smart_display.models import CalendarDaySection


GERMAN_WEEKDAYS_LONG = [
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
]


def compute_day_label(section_date_iso: str, today_iso: str) -> str:
    """Return the German label for a calendar section date.

    Rules:
    - ``section_date == today`` → "Heute · <Wochentag>"
    - ``section_date == today + 1`` → "Morgen · <Wochentag>"
    - ``section_date == today + 2`` → "Übermorgen · <Wochentag>"
    - anything else (future or past/stale) → full German weekday name

    Invalid ISO dates return an empty string; the caller decides how to render
    that (we don't want to crash the UI over a malformed cache payload).
    """
    try:
        section_date = date.fromisoformat(section_date_iso)
        today = date.fromisoformat(today_iso)
    except ValueError:
        return ""
    diff = (section_date - today).days
    weekday = GERMAN_WEEKDAYS_LONG[section_date.weekday()]
    if diff == 0:
        return f"Heute · {weekday}"
    if diff == 1:
        return f"Morgen · {weekday}"
    if diff == 2:
        return f"Übermorgen · {weekday}"
    return weekday


def apply_day_labels(
    sections: Iterable[CalendarDaySection], today_iso: str
) -> list[tuple[CalendarDaySection, str]]:
    """Pair each section with its computed label without mutating the section.

    Kept pure so tests can simulate midnight rollovers by just changing
    ``today_iso`` without having to rebuild the sections.
    """
    return [
        (section, compute_day_label(section.section_date, today_iso))
        for section in sections
    ]


def compute_row_budget(
    section_item_counts: Sequence[int],
    max_rows: int,
    *,
    section_has_label: Sequence[bool],
    section_extra_rows: Sequence[int] | None = None,
) -> list[int]:
    """Distribute ``max_rows`` across calendar sections with label overhead.

    Plan B7: the previous JS render broke on the first overflowing row and
    silently dropped every following section. The new behaviour allocates a
    row budget across all sections up-front so later days stay visible even
    if today is busy. The largest section shrinks first — this keeps the
    preview honest without starving any day entirely.

    Args:
        section_item_counts: original number of items per section, in the
            order they appear in the rendered list.
        max_rows: the total number of rows the list can physically show,
            counting label rows. Must be ≥ 0.
        section_has_label: whether each section consumes a dedicated label
            row. In the shipping UI this is ``True`` for every section —
            ``compute_day_label`` returns "Heute · <Wochentag>" for today too,
            so today gets a header row like every other day.
        section_extra_rows: extra rows a section costs while it shows at
            least one item. Homescreen v2 renders the next upcoming event
            as a taller highlighted row, so its section carries ``1`` here.
            ``None`` means no extras anywhere.

    Returns:
        A list of allocated item counts, same length as the input. Sections
        trimmed all the way to zero should have their label suppressed by
        the caller too.

    The JS counterpart in ``smart_display/web/static/js/app.js``
    (``computeRowBudget``) is a line-for-line port. Keep them in sync.
    """
    n = len(section_item_counts)
    if n == 0 or max_rows <= 0:
        return [0] * n
    if len(section_has_label) != n:
        raise ValueError(
            "section_has_label length must match section_item_counts"
        )

    extras = [0] * n if section_extra_rows is None else [max(0, int(x)) for x in section_extra_rows]
    if len(extras) != n:
        raise ValueError("section_extra_rows length must match section_item_counts")

    allocated = [max(0, int(count)) for count in section_item_counts]

    def total_rows() -> int:
        total = 0
        for i in range(n):
            if allocated[i] > 0:
                total += allocated[i] + extras[i]
                if section_has_label[i]:
                    total += 1
        return total

    # Greedy trim: repeatedly shave one item from the largest non-empty
    # section. For the inputs we see (≤ 3 sections, ≤ 30 items each) this is
    # effectively O(n²) on tiny n and simpler than a binary-search variant.
    #
    # `>=` breaks ties toward the LATEST section, which is what makes the result
    # usable: sections arrive chronologically, so with one appointment per day
    # (the common case) all sections tie, and picking the first would trim today
    # — the day that matters most — while keeping the day after tomorrow.
    guard = sum(allocated) + 1
    while total_rows() > max_rows and guard > 0:
        guard -= 1
        largest_idx = -1
        largest_value = 0
        for i in range(n):
            if allocated[i] > 0 and allocated[i] >= largest_value:
                largest_value = allocated[i]
                largest_idx = i
        if largest_idx < 0:
            break
        allocated[largest_idx] -= 1

    return allocated
