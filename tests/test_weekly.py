import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gold_realrate import make_weekly


class WeeklySemantics(unittest.TestCase):
    def test_common_endpoint_and_weekend_exclusion(self):
        gold = pd.Series([100., 105., 110., 999.], index=pd.to_datetime([
            "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-01"]))
        rates = pd.Series([2., 2.1], index=pd.to_datetime(["2026-10-28", "2026-10-29"]))
        _, _, w = make_weekly(gold, rates)
        self.assertEqual(w.iloc[0].gold_usd_oz, 105.)
        self.assertEqual(w.iloc[0].real_yield_pct, 2.1)
        self.assertEqual(w.iloc[0].observation_date, pd.Timestamp("2026-10-29"))

    def test_gap_not_collapsed_or_filled(self):
        dates = pd.to_datetime(["2026-10-16", "2026-10-30", "2026-11-06"])
        _, _, w = make_weekly(pd.Series([100., 120., 132.], dates), pd.Series([1., 2., 3.], dates))
        self.assertTrue(pd.isna(w.loc["2026-10-23", "gold_usd_oz"]))
        self.assertTrue(pd.isna(w.loc["2026-10-30", "gold_return"]))
        self.assertAlmostEqual(w.loc["2026-11-06", "gold_return"], .1)

    def test_partial_week_excluded(self):
        dates = pd.to_datetime(["2026-11-06", "2026-11-09"])
        _, all_weeks, complete = make_weekly(pd.Series([100., 101.], dates), pd.Series([2., 2.1], dates))
        self.assertEqual(len(all_weeks), 2)
        self.assertTrue(all_weeks.loc["2026-11-13", "partial_week"])
        self.assertEqual(complete.index.max(), pd.Timestamp("2026-11-06"))

    def test_requires_52_changes_and_matches_numpy(self):
        dates = pd.date_range("2003-01-03", periods=60, freq="W-FRI")
        prices = pd.Series(100 * np.exp(np.sin(np.arange(60) / 6) / 10), dates)
        rates = pd.Series(np.cos(np.arange(60) / 9), dates)
        _, _, w = make_weekly(prices, rates)
        self.assertTrue(w.corr_52w.iloc[:52].isna().all())
        expected = np.corrcoef(prices.pct_change(fill_method=None).iloc[1:53], rates.diff().iloc[1:53])[0, 1]
        self.assertAlmostEqual(w.corr_52w.iloc[52], expected, places=10)
        prices.iloc[30] = np.nan
        _, _, broken = make_weekly(prices, rates)
        self.assertTrue(broken.corr_52w.isna().all())


if __name__ == "__main__":
    unittest.main()
