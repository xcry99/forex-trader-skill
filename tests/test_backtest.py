import datetime
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import helpers  # noqa: E402

from fx import backtest, config, timeutil  # noqa: E402
from fx.fake import FakeGateway  # noqa: E402
from fx.symbols import SymbolSpec  # noqa: E402

CFG = config.load_config(trader_dir=os.path.join(ROOT, "tests", "_trader_bt"))


def _bar(t, o, h, l, c, spread=16):
    return {"t_utc": t, "o": o, "h": h, "l": l, "c": c, "spread": spread}


class TestSimulateExit(unittest.TestCase):
    def setUp(self):
        self.spec = SymbolSpec("EURUSD")
        self.t = helpers.NOW_UTC

    def test_target_first(self):
        bars = [_bar(self.t, 1.1020, 1.1041, 1.1019, 1.1030)]
        x = backtest.simulate_exit("buy", 1.1020, 1.1000, 1.1040, bars, self.spec)
        self.assertEqual(x["reason"], "target")
        self.assertAlmostEqual(x["price"], 1.1040, places=5)

    def test_stop_first(self):
        bars = [_bar(self.t, 1.1020, 1.1021, 1.0999, 1.1005)]
        x = backtest.simulate_exit("buy", 1.1020, 1.1000, 1.1040, bars, self.spec)
        self.assertEqual(x["reason"], "stop")
        self.assertAlmostEqual(x["price"], 1.1000, places=5)

    def test_stop_wins_the_tie(self):
        bars = [_bar(self.t, 1.1020, 1.1041, 1.0999, 1.1020)]
        x = backtest.simulate_exit("buy", 1.1020, 1.1000, 1.1040, bars, self.spec)
        self.assertEqual(x["reason"], "stop")

    def test_gap_through_the_stop(self):
        bars = [_bar(self.t, 1.0990, 1.0995, 1.0985, 1.0990)]
        x = backtest.simulate_exit("buy", 1.1020, 1.1000, 1.1040, bars, self.spec)
        self.assertEqual(x["reason"], "stop_gap")
        self.assertAlmostEqual(x["price"], 1.0990, places=5)

    def test_sell_pays_the_spread_on_the_ask(self):
        # bid high 1.10390 + 1.6 pips spread = 1.10406 >= stop 1.1040
        bars = [_bar(self.t, 1.1020, 1.1039, 1.1010, 1.1020)]
        x = backtest.simulate_exit("sell", 1.1020, 1.1040, 1.1000, bars, self.spec)
        self.assertEqual(x["reason"], "stop")
        self.assertAlmostEqual(x["price"], 1.1040, places=5)

    def test_flat_by_time_exit(self):
        bars = [_bar(self.t, 1.1020, 1.1025, 1.1015, 1.1022)]
        x = backtest.simulate_exit("buy", 1.1020, 1.1000, 1.1040, bars, self.spec)
        self.assertEqual(x["reason"], "flat_by")
        self.assertAlmostEqual(x["price"], 1.1022, places=5)


class TestBacktestRun(unittest.TestCase):
    def _gateway(self, symbols=("EURUSD",)):
        bars = helpers.make_orb_days_m5()
        d1 = helpers.make_d1(bars)
        rows = {}
        for s in symbols:
            rows[(s, "M5")] = bars
            rows[(s, "D1")] = d1
        g = FakeGateway(account={"balance": 1000.0, "equity": 1000.0}, bars=rows)
        for s in symbols:
            g.set_price(s, 1.1012, 1.1012)
        return g

    def test_orb_win_reaches_the_target(self):
        g = self._gateway()
        doc = backtest.run(CFG, g, symbols=["EURUSD"], days=8, daily=False,
                           now=helpers.NOW_UTC)
        self.assertEqual(len(doc["trades"]), 1, doc["skipped"])
        t = doc["trades"][0]
        self.assertEqual(t["setup"], "orb")
        self.assertEqual(t["side"], "buy")
        self.assertEqual(t["exit_reason"], "target")
        self.assertGreater(t["net"], 0)
        self.assertGreater(t["r_multiple"], 0)
        self.assertGreater(t["pips"], 0)
        self.assertIn("equity_curve", doc["summary"])
        self.assertEqual(doc["summary"]["wins"], 1)

    def test_orb_short_breakdown(self):
        bars = helpers.make_orb_days_m5(breakout="sell")
        d1 = helpers.make_d1(bars)
        g = FakeGateway(account={"balance": 1000.0, "equity": 1000.0},
                        bars={("EURUSD", "M5"): bars, ("EURUSD", "D1"): d1})
        g.set_price("EURUSD", 1.0988, 1.0988)
        doc = backtest.run(CFG, g, symbols=["EURUSD"], days=8, daily=False,
                           now=helpers.NOW_UTC)
        self.assertEqual(len(doc["trades"]), 1, doc["skipped"])
        self.assertEqual(doc["trades"][0]["side"], "sell")
        self.assertEqual(doc["trades"][0]["exit_reason"], "target")

    def test_max_trades_per_day(self):
        g = self._gateway(("EURUSD", "GBPUSD"))
        doc = backtest.run(CFG, g, symbols=["EURUSD", "GBPUSD"], days=8, daily=False,
                           now=helpers.NOW_UTC, max_trades=1)
        self.assertEqual(len(doc["trades"]), 1, doc["skipped"])
        stages = [s["stage"] for s in doc["skipped"]]
        self.assertIn("trades/day", stages)

    def test_no_lookahead_after_now(self):
        # The entry is at 03:35. Pinned to 03:40, the replay must stop there:
        # the trade cannot already know it would reach the target later.
        early = timeutil.ny_to_utc(datetime.datetime(2026, 11, 4, 3, 40))
        g = self._gateway()
        doc = backtest.run(CFG, g, symbols=["EURUSD"], days=8, daily=False, now=early)
        self.assertEqual(len(doc["trades"]), 1, doc["skipped"])
        t = doc["trades"][0]
        self.assertEqual(t["exit_reason"], "time")
        self.assertLessEqual(timeutil.parse_utc(t["closed"]), early)


if __name__ == "__main__":
    unittest.main(verbosity=2)
