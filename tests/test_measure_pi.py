from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_pi.py"
_spec = importlib.util.spec_from_file_location("measure_pi", _SCRIPT)
measure_pi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(measure_pi)


def _run(measured_at: str, age_s: int, swap_mb: float = 32.0) -> dict:
    return {
        "measured_at": measured_at,
        "processes": {
            "backend": [{"pid": 17915, "rss_mb": 31.0, "swap_mb": swap_mb, "age_s": age_s}],
            "cog": [{"pid": 17996, "rss_mb": 8.0, "swap_mb": 17.0, "age_s": age_s}],
        },
    }


class CompareTest(unittest.TestCase):
    def test_self_compare_is_zero(self) -> None:
        run = _run("2026-10-26T16:21:37+00:00", 17_000)
        rows = measure_pi.compare(run, copy.deepcopy(run))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertTrue(row["same"])
            self.assertEqual(row["delta_mb"], 0)

    def test_planted_growth_is_found(self) -> None:
        # 16 h later, same processes, the backend has 8 MB more in swap.
        earlier = _run("2026-10-26T16:00:00+00:00", 3_600)
        current = _run("2026-10-27T08:00:00+00:00", 3_600 + 16 * 3600, swap_mb=40.0)
        backend, cog = measure_pi.compare(earlier, current)
        self.assertEqual(backend["delta_mb"], 8.0)
        self.assertEqual(backend["mb_per_hour"], 0.5)
        self.assertEqual(cog["delta_mb"], 0)

    def test_rss_moving_to_swap_is_no_growth(self) -> None:
        earlier = _run("2026-10-26T16:00:00+00:00", 3_600)
        current = copy.deepcopy(earlier)
        current["measured_at"] = "2026-10-26T17:00:00+00:00"
        backend = current["processes"]["backend"][0]
        backend.update(age_s=7_200, rss_mb=21.0, swap_mb=42.0)
        self.assertEqual(measure_pi.compare(earlier, current)[0]["delta_mb"], 0)

    def test_restart_with_reused_pid_is_not_compared(self) -> None:
        earlier = _run("2026-10-26T16:00:00+00:00", 3_600)
        # One hour later the process is only ten minutes old: a deploy restarted it.
        current = _run("2026-10-26T17:00:00+00:00", 600)
        for row in measure_pi.compare(earlier, current):
            self.assertFalse(row["same"])
            self.assertNotIn("delta_mb", row)

    def test_unknown_pid_is_not_compared(self) -> None:
        earlier = _run("2026-10-26T16:00:00+00:00", 3_600)
        current = _run("2026-10-26T17:00:00+00:00", 7_200)
        current["processes"]["backend"][0]["pid"] = 20_001
        backend, cog = measure_pi.compare(earlier, current)
        self.assertFalse(backend["same"])
        self.assertTrue(cog["same"])

    def test_run_without_age_never_matches(self) -> None:
        earlier = _run("2026-10-26T16:00:00+00:00", 3_600)
        for entries in earlier["processes"].values():
            for entry in entries:
                del entry["age_s"], entry["swap_mb"]
        current = _run("2026-10-26T17:00:00+00:00", 7_200)
        self.assertFalse(any(row["same"] for row in measure_pi.compare(earlier, current)))


if __name__ == "__main__":
    unittest.main()
