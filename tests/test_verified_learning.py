"""Regression checks for strategy authorization, settlement, and optional replay."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from papertrader import Simulator, market

TREND = "600519"
PULLBACK = "300750"


def sample_market():
    dates = market._trading_days(24, end="2026-11-17")
    rows = []
    for code in (PULLBACK, TREND):
        for i, date in enumerate(dates):
            close = {19: 116, 20: 118}.get(i, 100 + i) if code == PULLBACK else 100 + i
            rows.append({"date": date, "code": code, "name": code, "close": close})
    return dates, rows


def buy(code):
    return {"action": "buy", "code": code, "weight": 0.10, "reason": "小仓试一下。"}


class TestVerifiedLearning(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.dates, self.rows = sample_market()

    def make_sim(self, name="account", **kwargs):
        options = {"offline": True, "seed": 31, "market_data": self.rows, **kwargs}
        sim = Simulator(self.folder / f"{name}.db", **options)
        self.addCleanup(sim.led.close)
        return sim

    def test_model_proposal_cannot_trade_the_other_arm(self):
        sim = self.make_sim(offline=False, audit=True)
        date = self.dates[19]
        policy = sim.brain.strategy_policy(sim.market, date)
        self.assertEqual(policy["candidates"], {"trend": [TREND], "pullback": [PULLBACK]})
        unwanted = next(code for code in (TREND, PULLBACK) if code not in policy["allowed_buys"])
        with patch.object(sim.brain, "_llm", return_value=(json.dumps(buy(unwanted)), "primary")) as call:
            result = sim.advance(date, auto_adapt=False)
        self.assertIn(policy["allowed_buys"][0], call.call_args.args[0][0]["content"])
        self.assertEqual(result["decision"]["brain"], "primary")
        self.assertEqual(result["result"]["gate"], "strategy")
        self.assertEqual(sim.led.positions(), [])
        self.assertEqual(sim.led.strategy_history()[-1]["sampled"], policy["draws"][policy["arm"]])
        self.assertTrue(sim.recheck(result["decision_id"])["matches_recorded"])

    def test_hold_is_an_action_and_does_not_teach_either_arm(self):
        sim = self.make_sim(proposer=lambda snap, actions: {"action": "hold", "reason": "我想再看看。"})
        outcome = sim.advance(self.dates[19])
        self.assertEqual(outcome["result"]["status"], "hold")
        self.assertEqual(sum(v["alpha"] + v["beta"] for v in sim.led.verified_strategy_stats().values()), 0)
        self.assertEqual(sim.diary(), [])

    def test_authorized_model_trade_teaches_the_original_arm(self):
        sim = self.make_sim(offline=False)
        start, exit_day = self.dates[19:21]
        policy = sim.brain.strategy_policy(self.rows, start)
        code = policy["allowed_buys"][0]
        sim.led.set_stance(start, code, "观望", intent="先看看")
        replies = [(json.dumps(buy(code)), "primary"),
                   (json.dumps({"action": "sell", "code": code, "reason": "按计划离场。"}), "secondary")]
        with patch.object(sim.brain, "_llm", side_effect=replies):
            entry = sim.advance(start)
            closed = sim.advance(exit_day)
        self.assertEqual(entry["result"]["status"], "filled")
        feedback = closed["result"]["feedback"]
        self.assertEqual(feedback["arm"], policy["arm"])
        self.assertEqual(feedback["brain"], "primary")
        self.assertEqual(feedback["decision_id"], entry["decision_id"])

    def test_exit_under_another_arm_rewards_only_the_entry_arm(self):
        sim = self.make_sim(offline=False)
        start, exit_day = self.dates[19:21]
        entry_policy = sim.brain.strategy_policy(self.rows, start)
        code = entry_policy["allowed_buys"][0]
        original = entry_policy["arm"]
        other = next(arm for arm in ("trend", "pullback") if arm != original)
        with patch.object(sim.brain, "_llm", return_value=(json.dumps(buy(code)), "primary")):
            opened = sim.advance(start, auto_adapt=False)
        before = sim.led.verified_strategy_stats()
        exit_policy = sim.brain.strategy_policy(self.rows, exit_day)
        exit_policy.update(arm=other, allowed_buys=exit_policy["candidates"][other])
        with patch.object(sim.brain, "strategy_policy", return_value=exit_policy), \
                patch.object(sim.brain, "_llm", return_value=(json.dumps({
                    "action": "sell", "code": code, "reason": "close with another policy"}), "secondary")):
            closed = sim.advance(exit_day, auto_adapt=False)
        feedback = closed["result"]["feedback"]
        self.assertEqual(closed["decision"]["strategy"], other)
        self.assertEqual(feedback["arm"], original)
        self.assertEqual(feedback["brain"], "primary")
        self.assertEqual(feedback["decision_id"], opened["decision_id"])
        self.assertEqual(feedback["entry_id"], opened["result"]["entry_id"])
        self.assertEqual(feedback["close_id"], closed["result"]["trade_id"])
        after = sim.led.verified_strategy_stats()
        expected = dict(before[original])
        expected["alpha" if closed["result"]["pnl_pct"] > 0 else "beta"] += 1
        self.assertEqual(after[original], expected)
        self.assertEqual(after[other], before[other])

    def test_explicit_commitment_is_scoped_and_replay_changes_only_the_gate(self):
        sim = self.make_sim(audit=True, proposer=lambda snap, actions: buy(actions["buy"][0]))
        date = self.dates[19]
        code = sim.brain.strategy_policy(sim.market, date)["allowed_buys"][0]
        commitment = sim.led.commit_to_wait(date, code, "今天这只不开新仓。")
        outcome = sim.advance(date)
        self.assertEqual(outcome["result"]["gate"], "commitment")
        before = sim.led.journal()
        replay = sim.recheck(outcome["decision_id"], ignore_commitment=True)
        self.assertTrue(replay["matches_recorded"])
        self.assertFalse(replay["baseline"]["allowed"])
        self.assertTrue(replay["rechecked"]["allowed"])
        self.assertEqual(sim.led.journal(), before)
        self.assertEqual(sim.diary()[0]["facts"]["quote"], "今天这只不开新仓。")
        sim.led.revise_commitment(commitment, date, "原先假设已经变化。")
        self.assertFalse(sim.led.commitments_on(date))
        self.assertEqual(sim.led.commitment(commitment)["revision"], "原先假设已经变化。")

    def test_next_close_pays_costs_and_feedback_is_atomic_and_idempotent(self):
        start, entry_day, exit_day = self.dates[19:22]

        def proposer(snapshot, actions):
            if snapshot["date"] == start:
                return buy(actions["buy"][0])
            if actions["sell"]:
                return {"action": "sell", "code": actions["sell"][0], "reason": "按计划离场。"}
            return {"action": "hold"}

        sim = self.make_sim(execution="next_close", fee_bps=10, proposer=proposer, audit=True)
        first = sim.advance(start, auto_adapt=False)
        self.assertEqual(first["result"]["status"], "pending")
        self.assertEqual(sim.led.positions(), [])
        sim.led.close()
        with self.assertRaisesRegex(ValueError, "成交时点"):
            sim.advance(exit_day, auto_adapt=False)
        second = sim.advance(entry_day, auto_adapt=False)
        self.assertEqual(second["settlement"]["price"],
                         market.close_on(self.rows, entry_day, first["decision"]["code"]))
        self.assertGreater(second["settlement"]["fee"], 0)
        conn = sim.led.conn()
        conn.execute("CREATE TRIGGER fail_outcome BEFORE UPDATE OF outcome ON decisions "
                     "BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            sim.advance(exit_day, auto_adapt=False)
        self.assertTrue(sim.led.positions())
        self.assertIsNone(sim.led.decision(second["decision_id"])["outcome"])
        self.assertEqual(sim.led.verified_strategy_stats()[first["decision"]["strategy"]]["alpha"], 0)
        conn.execute("DROP TRIGGER fail_outcome")
        conn.commit()
        third = sim.advance(exit_day, auto_adapt=False)
        sale = third["settlement"]
        feedback = sale["feedback"]
        self.assertEqual(feedback["entry_id"], second["settlement"]["entry_id"])
        self.assertEqual(feedback["arm"], first["decision"]["strategy"])
        self.assertEqual(feedback["after"], {"alpha": 1, "beta": 0})
        gross = (sale["price"] / second["settlement"]["price"] - 1) * 100
        self.assertLess(sale["pnl_pct"], gross)
        self.assertTrue(sim.recheck(second["decision_id"], stage="execution")["matches_recorded"])
        self.assertEqual(sim.led.execute(None, self.rows, exit_day, decision_id=second["decision_id"])["feedback"], feedback)
        self.assertEqual(sim.led.verified_strategy_stats()[feedback["arm"]]["alpha"], 1)
        self.assertEqual(sim.diary()[0]["type"], "feedback")
        self.assertTrue(sim.led.verify_chain()[0])

    def test_missing_execution_bar_never_uses_yesterday_price(self):
        date, following = self.dates[19:21]
        chosen = self.make_sim("selector").brain.strategy_policy(self.rows, date)["allowed_buys"][0]
        rows = [r for r in self.rows if (r["date"], r["code"]) != (following, chosen)]
        sim = self.make_sim("missing", execution="next_close", market_data=rows,
                            proposer=lambda snap, actions: buy(chosen) if snap["date"] == date else {"action": "hold"})
        self.assertEqual(sim.advance(date)["result"]["status"], "pending")
        self.assertEqual(sim.advance(following)["settlement"]["gate"], "quote")
        self.assertEqual(sim.led.positions(), [])

    def test_future_rows_and_audit_switch_do_not_change_current_decision(self):
        date = self.dates[19]
        prefix = [r for r in self.rows if r["date"] <= date]
        first = self.make_sim("prefix", market_data=prefix, audit=False)
        second = self.make_sim("full", audit=True)
        a = first.advance(date)
        b = second.advance(date)
        self.assertEqual(a, b)
        self.assertEqual(first.overview()["equity"], second.overview()["equity"])
        self.assertIsNone(first.led.decision(a["decision_id"])["audit"])
        self.assertIn("agent_input", second.led.decision(b["decision_id"])["audit"])

    def test_offline_mode_never_calls_model_even_with_key(self):
        with patch.dict("os.environ", {"ZHIPU_API_KEY": "dummy"}):
            sim = self.make_sim()
            with patch.object(sim.brain, "_call", side_effect=AssertionError("model called")) as call:
                self.assertTrue(sim.advance(self.dates[19])["decision"]["brain"].startswith("local("))
                sim.diary(use_model=True)
                call.assert_not_called()
