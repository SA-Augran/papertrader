# -*- coding: utf-8 -*-
"""核心机制测试：账本 / 规则闸门 / hash 链 / 自适应 / 立场账本。用临时库，不碰任何生产数据。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from papertrader.core import Ledger, GENESIS_HASH, ADAPT_MIN_CLOSED
from papertrader import market as M


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.gettempdir()) / f"pt_test_{id(self)}.db"
        if self.db.exists():
            self.db.unlink()
        self.led = Ledger(self.db)
        self.led.init_account()
        self.market = M.synthetic(n=60)
        self.dates = M.market_dates(self.market)
        self.date = self.dates[-1]

    def tearDown(self):
        self.led.close()
        if self.db.exists():
            self.db.unlink()

    def buy(self, code="600519", weight=0.15, date=None, block_date=None, **kw):
        p = {"action": "buy", "code": code, "weight": weight,
             "reason": "测试买入", "confidence": 0.6}
        p.update(kw)
        return self.led.execute(p, self.market, date or self.date, block_date=block_date)

    def test_initial_account(self):
        self.assertAlmostEqual(self.led.cash(), 100000.0)
        self.assertEqual(len(self.led.watchlist()), 8)
        self.assertEqual(self.led.active_rules()["max_weight"], 0.25)

    def test_buy_updates_positions_and_nav(self):
        r = self.buy()
        self.assertTrue(r["executed"])
        self.assertEqual(len(self.led.positions()), 1)
        self.assertLess(self.led.cash(), 100000.0)
        self.assertEqual(len(self.led.nav_series()), 1)

    def test_rule_gate_rejects_oversize(self):
        r = self.buy(weight=0.30)
        self.assertFalse(r["executed"])
        self.assertIn("仓位", r["reject_reason"])
        self.assertEqual(len(self.led.positions()), 0)

    def test_rule_gate_rejects_out_of_pool(self):
        r = self.buy(code="999999")
        self.assertFalse(r["executed"])

    def test_rule_gate_rejects_no_reason(self):
        r = self.buy(reason="")
        self.assertFalse(r["executed"])

    def test_hold_needs_no_gate(self):
        r = self.led.execute({"action": "hold", "reason": ""}, self.market, self.date)
        self.assertTrue(r["executed"])

    def test_block_date_rejects_trades(self):
        r = self.buy(date=self.date, block_date=self.date)
        self.assertFalse(r["executed"])
        self.assertIn("不动手", r["reject_reason"])

    def test_sell_ignores_weight_cap(self):
        # 卖出提案 weight=0 是合法的，不应被"仓位必须在 0~25%"挡回
        self.buy()  # 先建一手仓
        sell = {"action": "sell", "code": "600519", "weight": 0,
                "reason": "测试卖出"}
        ok, why, _ = self.led.check_rules(sell, self.market, self.date)
        self.assertTrue(ok, f"sell with weight=0 should pass, got: {why}")

    def test_hash_chain_valid_then_tamper_detected(self):
        self.buy()
        self.buy("300750", 0.10)
        ok, broken = self.led.verify_chain()
        self.assertTrue(ok, f"chain should be valid, broken at {broken}")
        # 篡改中间一条的 reason
        row = self.led.conn().execute("SELECT id,hash FROM trades ORDER BY id LIMIT 1").fetchone()
        self.led.conn().execute("UPDATE trades SET reason='被篡改' WHERE id=?", (row["id"],))
        self.led.conn().commit()
        ok, broken = self.led.verify_chain()
        self.assertFalse(ok)
        self.assertEqual(broken, row["id"])

    def test_hash_chain_genesis(self):
        ok, _ = self.led.verify_chain()
        self.assertTrue(ok)  # 空链也合法

    def test_stance_write_supersedes_and_expiry(self):
        self.led.set_stance(self.date, "600519", "观望", days=1)
        rows = self.led.active_stances(self.date)
        self.assertEqual(len(rows), 1)
        self.led.set_stance(self.date, "600519", "看多", days=5)
        rows = self.led.active_stances(self.date)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "看多")
        self.assertIn("600519", self.led.stance_line(self.date))

    def test_stance_expiry(self):
        self.led.set_stance("2026-01-01", "600519", "看多", days=1)  # expire 01-02
        self.assertEqual(len(self.led.active_stances("2026-01-02")), 1)
        self.assertEqual(len(self.led.active_stances("2026-01-03")), 0)

    def test_consistency_conflict_logged(self):
        self.led.set_stance(self.date, "600519", "观望", days=2)
        conflict, note = self.led.note_stance_consistency(self.date,
            {"action": "buy", "code": "600519"})
        self.assertTrue(conflict)
        self.assertIn("改口", note)
        advice = self.led.conn().execute("SELECT count(*) c FROM advice").fetchone()["c"]
        self.assertEqual(advice, 1)

    def test_adapt_rules_needs_samples(self):
        r = self.led.adapt_rules()
        self.assertFalse(r["changed"])
        self.assertIn("维持", r["note"])

    def test_same_closed_samples_never_repeatedly_tighten_rules(self):
        for _ in range(8):
            self.led.conn().execute(
                "INSERT INTO trades(date,code,action,reason,result) VALUES(?,?,?,?,?)",
                ("2025-01-02", "600519", "sell", "退出", "-2.0%"))
        self.led.conn().commit()
        first = self.led.adapt_rules(date="2025-01-02")
        self.assertTrue(first["changed"])
        history = self.led.rule_history()
        second = self.led.adapt_rules(date="2025-01-03")
        self.assertFalse(second["changed"])
        self.assertEqual(first["rules"], second["rules"])
        self.assertEqual(history, self.led.rule_history())
        self.led.conn().execute(
            "INSERT INTO trades(date,code,action,reason,result) VALUES(?,?,?,?,?)",
            ("2025-01-06", "600519", "sell", "退出", "-2.0%"))
        self.led.conn().commit()
        self.assertTrue(self.led.adapt_rules(date="2025-01-06")["changed"])

    def test_adapt_rules_bounds_and_single_step(self):
        # 造 10 笔亏损平仓，触发收紧；验证不越界、单次只动一步
        for i in range(10):
            self.led.conn().execute(
                "INSERT INTO trades(date,code,action,reason,result,prev_hash,hash) "
                "VALUES(?,?,?,?,?,?,?)",
                (f"2026-01-{i+1:02d}", "600519", "sell", "x", "-5.0%", GENESIS_HASH, "x"))
        self.led.conn().commit()
        before = self.led.active_rules()
        r = self.led.adapt_rules()
        self.assertTrue(r["changed"])
        after = r["rules"]
        self.assertGreaterEqual(after["chase_limit_pct"], 6.0)
        self.assertLessEqual(after["chase_limit_pct"], before["chase_limit_pct"])
        self.assertAlmostEqual(after["chase_limit_pct"], before["chase_limit_pct"] - 1.0)

    def test_adapt_rules_recovers_with_window(self):
        # 死锁回归：先造 10 笔亏损把规则压到边界，再造 12 笔盈利——
        # 滚动窗口应让规则回升；全历史累计口径会永远卡在边界（旧 bug）。
        for i in range(10):
            self.led.conn().execute(
                "INSERT INTO trades(date,code,action,reason,result) VALUES(?,?,?,?,?)",
                (f"2026-01-{i+1:02d}", "600519", "sell", "x", "-5.0%"))
        self.led.meta_set("active_rules", '{"max_weight":0.2,"first_buy_cap":0.12,"chase_limit_pct":6.0}')
        for i in range(12):
            self.led.conn().execute(
                "INSERT INTO trades(date,code,action,reason,result) VALUES(?,?,?,?,?)",
                (f"2026-02-{i+1:02d}", "600519", "sell", "x", "+6.0%"))
        self.led.conn().commit()
        r = self.led.adapt_rules()
        self.assertTrue(r["changed"])
        self.assertGreater(r["rules"]["chase_limit_pct"], 6.0)

    def test_adapt_rules_no_false_change_at_bound(self):
        # 已在上边界 + 低亏损率信号（放宽方向）=> 到顶了，不该再记一次 changed
        self.led.meta_set("active_rules", '{"max_weight":0.30,"first_buy_cap":0.18,"chase_limit_pct":12.0}')
        for i in range(10):
            self.led.conn().execute(
                "INSERT INTO trades(date,code,action,reason,result) VALUES(?,?,?,?,?)",
                (f"2026-01-{i+1:02d}", "600519", "sell", "x", "+3.0%"))
        self.led.conn().commit()
        r = self.led.adapt_rules()
        self.assertFalse(r["changed"])

    def test_synthetic_is_deterministic(self):
        a = M.synthetic(n=30, end="2026-01-01")
        b = M.synthetic(n=30, end="2026-01-01")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
