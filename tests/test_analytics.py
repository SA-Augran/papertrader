# -*- coding: utf-8 -*-
"""analytics 与规则演化轨迹测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from papertrader import analytics


def nav(vals):
    return [{"date": f"2026-01-{i+1:02d}", "total": v, "cash": v} for i, v in enumerate(vals)]


class TestAnalytics(unittest.TestCase):
    def test_empty(self):
        m = analytics.metrics([])
        self.assertEqual(m["days"], 0)
        self.assertIsNone(m["sharpe"])

    def test_flat_series(self):
        m = analytics.metrics(nav([100, 100, 100, 100]))
        self.assertEqual(m["total_return_pct"], 0.0)
        self.assertIsNone(m["sharpe"])  # 零波动无夏普（除零保护）
        self.assertEqual(m["max_drawdown_pct"], 0.0)

    def test_drawdown(self):
        # 100 → 120 → 90 → 110：峰值 120 谷底 90 = -25%
        m = analytics.metrics(nav([100, 120, 90, 110]))
        self.assertAlmostEqual(m["max_drawdown_pct"], -25.0, places=1)

    def test_total_return(self):
        m = analytics.metrics(nav([100, 110]))
        self.assertAlmostEqual(m["total_return_pct"], 10.0, places=2)

    def test_positive_sharpe_on_uptrend(self):
        m = analytics.metrics(nav([100, 102, 104, 106, 108, 110]))
        self.assertGreater(m["sharpe"], 0)

    def test_var_is_positive(self):
        m = analytics.metrics(nav([100, 101, 99, 102, 98, 103, 97]))
        self.assertIsNotNone(m["var95_daily_pct"])
        self.assertGreaterEqual(m["var95_daily_pct"], 0)

    def test_trade_stats(self):
        trades = [
            {"action": "sell", "result": "+5.0%"},
            {"action": "sell", "result": "-2.0%"},
            {"action": "buy", "result": "持有中"},
        ]
        s = analytics.trade_stats(trades)
        self.assertEqual(s["closed"], 2)
        self.assertEqual(s["win_rate_pct"], 50.0)
        self.assertEqual(s["profit_factor"], 2.5)

    def test_rule_evolution_shape(self):
        hist = [
            {"date": "2026-01-01", "max_weight": 0.25, "first_buy_cap": 0.15, "chase_limit_pct": 9.0},
            {"date": "2026-01-02", "max_weight": 0.24, "first_buy_cap": 0.14, "chase_limit_pct": 8.0},
        ]
        s = analytics.rule_evolution(hist)
        self.assertEqual(s["date"], ["2026-01-01", "2026-01-02"])
        self.assertEqual(s["max_weight"], [0.25, 0.24])

    def test_compare_runs(self):
        c = analytics.compare_runs(nav([100, 110]), nav([100, 105]))
        self.assertEqual(len(c["rows"]), 5)
        self.assertEqual(c["labels"], ["固定规则", "自适应"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
