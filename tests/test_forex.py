import argparse
import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "trader-entry"))
sys.path.insert(0, os.path.join(ROOT, "trader-timemachine"))
sys.path.insert(0, os.path.join(ROOT, "trader-alerts"))
sys.path.insert(0, os.path.join(ROOT, "trader-broker"))

import helpers  # noqa: E402
import entry as entry_mod  # noqa: E402
import timemachine as tm_mod  # noqa: E402
import alerts as alerts_mod  # noqa: E402
import broker as broker_mod  # noqa: E402

from fx import config, indicators, journal, plan, risk, sizing, store, timeutil  # noqa: E402
from fx.symbols import SymbolSpec  # noqa: E402


CFG = config.load_config(trader_dir=os.path.join(ROOT, "tests", "_trader"))


class TestPipsAndPlan(unittest.TestCase):
    def test_cli_table_left_and_right(self):
        from fx import cli
        text = cli.table([["EURUSD", 1.1]], ["SYMBOL", "PRICE"], aligns=["<", ">"])
        self.assertIn("SYMBOL", text)
        self.assertIn("EURUSD", text)
        self.assertIn(" 1.1", text)

    def test_pip_sizes(self):
        eur = SymbolSpec("EURUSD", digits=5, point=0.00001)
        jpy = SymbolSpec("USDJPY", digits=3, point=0.001)
        self.assertAlmostEqual(eur.pip_size, 0.0001)
        self.assertAlmostEqual(jpy.pip_size, 0.01)
        self.assertAlmostEqual(eur.pips(0.0020), 20.0)
        self.assertAlmostEqual(jpy.pips(0.20), 20.0)

    def test_plan_long(self):
        spec = SymbolSpec("EURUSD")
        p = plan.compute(1.1020, "buy", "pips", 20, "r", 2.0, spec)
        self.assertAlmostEqual(p["stop"], 1.1000, places=5)
        self.assertAlmostEqual(p["target"], 1.1060, places=5)
        self.assertEqual(p["risk_pips"], 20.0)
        self.assertEqual(p["reward_risk"], 2.0)

    def test_plan_short(self):
        spec = SymbolSpec("EURUSD")
        p = plan.compute(1.1020, "sell", "pips", 20, "r", 1.0, spec)
        self.assertAlmostEqual(p["stop"], 1.1040, places=5)
        self.assertAlmostEqual(p["target"], 1.1000, places=5)

    def test_plan_rejects_wrong_side(self):
        spec = SymbolSpec("EURUSD")
        with self.assertRaises(plan.PlanError):
            plan.compute(1.1020, "buy", "price", 1.1030, "r", 1.0, spec)

    def test_min_risk_floor(self):
        spec = SymbolSpec("EURUSD")
        with self.assertRaises(plan.PlanError):
            plan.compute(1.1020, "buy", "pips", 0.5, "r", 1.0, spec)


class TestSizing(unittest.TestCase):
    def setUp(self):
        self.g = helpers.make_gateway()
        self.spec = self.g.symbol("EURUSD")

    def _plan(self, side="buy", entry=1.1020, stop=1.1000, target=1.1040):
        return {"symbol": "EURUSD", "side": side, "entry": entry, "stop": stop,
                "target": target, "setup": "orb", "session": "london"}

    def test_long_005_lots(self):
        s = sizing.size_plan(self._plan(), CFG, self.spec, self.g)
        self.assertAlmostEqual(s["volume"], 0.05, places=2)
        self.assertAlmostEqual(s["max_loss"], 10.0, places=1)
        self.assertAlmostEqual(s["margin"], 5.51, places=1)
        self.assertIsNone(s["refusal"])

    def test_short_005_lots(self):
        s = sizing.size_plan(self._plan("sell", 1.1020, 1.1040, 1.1000), CFG, self.spec, self.g)
        self.assertAlmostEqual(s["volume"], 0.05, places=2)
        self.assertAlmostEqual(s["max_loss"], 10.0, places=1)

    def test_jpy_risk_per_lot(self):
        g = helpers.make_gateway()
        spec = g.symbol("USDJPY")
        g.set_price("USDJPY", 158.000, 158.000)
        p = {"symbol": "USDJPY", "side": "buy", "entry": 158.000, "stop": 157.800,
             "target": 158.200, "setup": "orb", "session": "london"}
        s = sizing.size_plan(p, CFG, spec, g)
        self.assertGreater(s["volume"], 0.05)
        self.assertLessEqual(s["max_loss"], 10.01)

    def test_refuses_when_min_lot_risks_too_much(self):
        p = self._plan(stop=1.0800, target=1.1240)  # 220 pips
        s = sizing.size_plan(p, CFG, self.spec, self.g)
        self.assertEqual(s["volume"], 0.0)
        self.assertIn("minimum lot", s["refusal"])

    def test_bracket_shape(self):
        s = sizing.size_plan(self._plan(), CFG, self.spec, self.g)
        o = sizing.bracket_order(s)
        self.assertEqual(o["symbol"], "EURUSD")
        self.assertEqual(o["side"], "buy")
        self.assertIn("sl", o)
        self.assertIn("tp", o)


class TestRisk(unittest.TestCase):
    def setUp(self):
        self.g = helpers.make_gateway()
        self.now = helpers.NOW_UTC

    def _check(self, orders, state=None, now=None):
        return risk.check(orders, CFG, self.g, state=state or {}, now=now or self.now)

    def test_pass(self):
        r = self._check([helpers.make_order()])
        self.assertEqual(r["pass"], 1, r["results"][0]["reasons"])

    def test_block_max_loss(self):
        r = self._check([helpers.make_order(volume=0.10)])
        self.assertEqual(r["block"], 1)
        self.assertTrue(any("max loss" in x for x in r["results"][0]["reasons"]))

    def test_block_reward_risk(self):
        r = self._check([helpers.make_order(target=1.1030)])  # 10 pips reward / 20 risk
        self.assertEqual(r["block"], 1)
        self.assertTrue(any("reward:risk" in x for x in r["results"][0]["reasons"]))

    def test_block_outside_window(self):
        late = timeutil.ny_to_utc(datetime.datetime(2026, 11, 4, 13, 0))
        r = self._check([helpers.make_order()], now=late)
        self.assertEqual(r["block"], 1)
        self.assertTrue(any("window" in x for x in r["results"][0]["reasons"]))

    def test_block_market_closed(self):
        sat = timeutil.ny_to_utc(datetime.datetime(2026, 11, 7, 10, 0))
        r = self._check([helpers.make_order()], now=sat)
        self.assertEqual(r["block"], 1)
        self.assertTrue(any("market" in x for x in r["results"][0]["reasons"]))

    def test_exit_not_blocked_by_entry_rules(self):
        self.g.open_order("EURUSD", "buy", 0.05, sl=1.1000, tp=1.1040)
        pos = self.g.positions()
        late = timeutil.ny_to_utc(datetime.datetime(2026, 11, 4, 13, 0))
        order = helpers.make_order(side="sell", volume=0.05)
        order.pop("sl")
        order.pop("tp")
        r = self._check([order], now=late)
        self.assertEqual(r["results"][0]["kind"], "exit")
        self.assertEqual(r["pass"], 1, r["results"][0]["reasons"])

    def test_duplicate_same_day(self):
        state = {"orders": [{"symbol": "EURUSD", "side": "buy", "status": "placed",
                             "date": "2026-11-04", "max_loss": 10.0, "kind": "entry"}]}
        r = self._check([helpers.make_order()], state=state)
        self.assertEqual(r["block"], 1)
        self.assertTrue(any("duplicate" in x for x in r["results"][0]["reasons"]))

    def test_daily_loss_limit(self):
        state = {"orders": [{"symbol": "GBPUSD", "side": "buy", "status": "placed",
                             "date": "2026-11-04", "max_loss": 25.0, "kind": "entry"}]}
        r = self._check([helpers.make_order()], state=state)
        self.assertEqual(r["block"], 1)
        self.assertTrue(any("daily loss" in x for x in r["results"][0]["reasons"]))


class TestIndicators(unittest.TestCase):
    def test_rsi_bounds(self):
        up = list(range(1, 40))
        self.assertGreater(indicators.rsi(up), 70)
        down = list(range(40, 1, -1))
        self.assertLess(indicators.rsi(down), 30)

    def test_breakout_and_breakdown(self):
        closes = [1.10] * 20 + [1.12]
        tags, _ = indicators.find_setups(closes)
        self.assertIn("breakout_20d", tags["long"])
        closes = [1.10] * 20 + [1.08]
        tags, _ = indicators.find_setups(closes)
        self.assertIn("breakdown_20d", tags["short"])

    def test_opening_range(self):
        bars = helpers.make_m5()
        start, end = timeutil.session_window(helpers.NOW_NY.date(), "london")
        rng = indicators.opening_range(bars, start, 30, helpers.NOW_UTC, 5, rest_end=end)
        self.assertIsNotNone(rng)
        self.assertGreater(rng["high"], rng["low"])
        self.assertTrue(rng["rest"])

    def test_first_break_long_and_short(self):
        bars = helpers.make_m5()
        start, end = timeutil.session_window(helpers.NOW_NY.date(), "london")
        rng = indicators.opening_range(bars, start, 30, helpers.NOW_UTC, 5, rest_end=end)
        br = indicators.first_break(rng["rest"], rng["high"], rng["low"])
        self.assertEqual(br["side"], "buy")


class TestEntry(unittest.TestCase):
    def test_orb_triggered(self):
        g = helpers.make_gateway()
        spec = g.symbol("EURUSD")
        e = entry_mod.evaluate(g, spec, helpers.NOW_UTC, setup="orb", side="auto",
                               session="london", or_minutes=30)
        self.assertEqual(e["status"], "triggered")
        self.assertEqual(e["side"], "buy")
        self.assertGreater(e["price"], e["or_high"])

    def test_short_breakdown(self):
        bars = helpers.make_m5(breakout_side="sell")
        g = helpers.make_gateway(bars=bars)
        g.set_price("EURUSD", 1.0980, 1.0980)
        spec = g.symbol("EURUSD")
        e = entry_mod.evaluate(g, spec, helpers.NOW_UTC, setup="orb", side="auto",
                               session="london", or_minutes=30)
        self.assertEqual(e["status"], "triggered")
        self.assertEqual(e["side"], "sell")


class TestTimeMachine(unittest.TestCase):
    def test_monte_carlo_sums_to_one_and_is_deterministic(self):
        g = helpers.make_gateway()
        p = {"symbol": "EURUSD", "side": "buy", "entry": 1.1020, "stop": 1.1000,
             "target": 1.1040, "session": "london",
             "entry_time": timeutil.iso(timeutil.ny_to_utc(datetime.datetime(2026, 11, 4, 3, 35)))}
        a = tm_mod.compute(g, p, CFG, helpers.NOW_UTC, replays=2000, seed=19, days=60)
        b = tm_mod.compute(g, p, CFG, helpers.NOW_UTC, replays=2000, seed=19, days=60)
        mc = a["monte_carlo"]
        self.assertAlmostEqual(mc["target_first"] + mc["stop_first"] + mc["open_at_end"], 1.0, places=3)
        self.assertEqual(mc["target_first"], b["monte_carlo"]["target_first"])
        self.assertIn(a["verdict"], ("positive edge", "negative edge", "coin flip"))


class TestTimeMachineFlatBy(unittest.TestCase):
    def test_session_bars_run_to_flat_by_not_the_london_close(self):
        bars = helpers.make_flat_day_m5()
        day = helpers.NOW_NY.date()
        sb, _, end = tm_mod._session_bars_for(bars, day, "london", flat_by="16:55")
        self.assertEqual(end, timeutil.flat_by_utc(day, "16:55"))
        self.assertGreater(sb[-1]["t_utc"], timeutil.session_close_utc(day, "london"))
        plain, _, plain_end = tm_mod._session_bars_for(bars, day, "london")
        self.assertEqual(plain_end, timeutil.session_close_utc(day, "london"))
        self.assertGreater(len(sb), len(plain))

    def test_history_replay_reaches_flat_by(self):
        bars = helpers.make_flat_day_m5()
        g = helpers.make_gateway(bars=bars)
        plan_doc = {
            "symbol": "EURUSD", "side": "buy", "entry": 1.1000,
            "stop": 1.0980, "target": 1.1020, "session": "london",
            "entry_time": timeutil.iso(timeutil.ny_to_utc(
                datetime.datetime(2026, 11, 4, 10, 0))),
        }
        doc = tm_mod.compute(g, plan_doc, CFG, helpers.NOW_UTC, replays=200, seed=19, days=25)
        # The target is only touched after the London close: a replay that
        # stopped at 12:00 NY would leave every session open.
        self.assertGreater(doc["bars_left"], 60)
        self.assertEqual(doc["history"]["open"], 0)
        self.assertGreaterEqual(doc["history"]["target"], 15)


class TestJournal(unittest.TestCase):
    def test_sync_and_metrics(self):
        g = helpers.make_gateway()
        g.set_price("EURUSD", 1.1000, 1.1000)
        res = g.open_order("EURUSD", "buy", 0.05, sl=1.0980, tp=1.1040)
        ticket = res["position"]
        g.set_price("EURUSD", 1.1020, 1.1020)
        g.close_position(ticket, comment="tp")
        state = {"orders": [{"position": ticket, "max_loss": 10.0, "setup": "orb",
                             "session": "london", "kind": "entry"}]}
        tmp = os.path.join(ROOT, "tests", "_trader_journal")
        cfg = config.load_config(trader_dir=tmp)
        shutil.rmtree(tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, tmp, True)
        out = journal.sync(g, cfg, state, days=1)
        self.assertEqual(out["added"], 1)
        recs = journal.store.read_jsonl(cfg, "journal.jsonl")
        self.assertEqual(len(recs), 1)
        m = journal.metrics(recs, 1000.0)
        self.assertEqual(m["trades"], 1)
        self.assertGreater(m["net"], 0)
        self.assertEqual(m["wins"], 1)
        self.assertAlmostEqual(m["r_multiple"] if "r_multiple" in m else recs[0]["r_multiple"], 1.0, places=1)


class TestAutoNews(unittest.TestCase):
    """The autonomous pipeline and how trader/news.json is used inside it."""

    def setUp(self):
        self.tmp = os.path.join(ROOT, "tests", "_trader_auto")
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.cfg = config.load_config(trader_dir=self.tmp)
        self.cfg["strategy"]["timemachine"]["replays"] = 200
        self.g = helpers.make_gateway()
        self.now = helpers.NOW_UTC
        self.args = argparse.Namespace(allow_live=False, session=None, or_minutes=None,
                                       max_candidates=1, news=False, dry_run=True, interval=1,
                                       now=timeutil.iso(self.now))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _news_doc(self, impact="high", published=None):
        title = "ECB rate decision: euro on hold"
        item = {"title": title, "link": None, "published": timeutil.iso(published or self.now),
                "summary": "", "source": "test", "currencies": ["EUR", "USD"],
                "impact": impact, "tags": ["rate"]}
        slot = {"items": 1, "high": 1 if impact == "high" else 0,
                "medium": 1 if impact == "medium" else 0,
                "titles": [title], "risk": impact}
        return {"as_of": timeutil.iso(self.now), "hours": 24, "items": [item],
                "currencies": {"EUR": dict(slot), "USD": dict(slot)}, "errors": {}}

    def _run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = broker_mod.cmd_auto(self.args, self.cfg, self.g, self.now, once=True)
        return code, buf.getvalue()

    def test_high_impact_blackout_only_when_configured(self):
        store.save_json(self.cfg, "news.json", self._news_doc())
        self.cfg["strategy"]["news"]["avoid_high_impact_minutes"] = 30
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIsNone(store.load_json(self.cfg, "orders.json"))
        decisions = store.read_jsonl(self.cfg, "decisions.jsonl")
        self.assertTrue(any(d["type"] == "skipped" and d.get("stage") == "news"
                            for d in decisions), decisions)
        self.assertIn("no tradable order this cycle", out)

    def test_news_is_context_only_by_default(self):
        store.save_json(self.cfg, "news.json", self._news_doc())
        self.assertEqual(self.cfg["strategy"]["news"]["avoid_high_impact_minutes"], 0)
        code, _ = self._run()
        self.assertEqual(code, 0)
        orders = store.load_json(self.cfg, "orders.json") or []
        self.assertTrue(orders, "avoid_high_impact_minutes=0 must not block the trade")
        self.assertEqual(orders[0]["plan"]["news"]["risk"], "high")
        self.assertIn("timemachine", orders[0]["plan"])
        decisions = store.read_jsonl(self.cfg, "decisions.jsonl")
        self.assertTrue(any(d["type"] == "candidate" for d in decisions), decisions)


class TestAlerts(unittest.TestCase):
    def test_stop_target_time(self):
        q = {"EURUSD": {"bid": 1.1000, "ask": 1.1002, "mid": 1.1001,
                        "pip_size": 0.0001, "spread_pips": 2.0}}
        stop = {"type": "stop", "symbol": "EURUSD", "price": 1.1005, "side": "buy"}
        target = {"type": "target", "symbol": "EURUSD", "price": 1.1005, "side": "sell"}
        time_rule = {"type": "time", "at": "08:00"}
        now = timeutil.ny_to_utc(datetime.datetime(2026, 11, 4, 9, 0))
        self.assertTrue(alerts_mod.evaluate(stop, q, now)[0])
        self.assertTrue(alerts_mod.evaluate(target, q, now)[0])
        self.assertTrue(alerts_mod.evaluate(time_rule, q, now)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
