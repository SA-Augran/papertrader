# -*- coding: utf-8 -*-
"""Thompson Sampling 策略学习测试：采样选优 / 反馈更新 / 偏好曲线 / 可复现。"""
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from papertrader import Simulator, strategy as ST
from papertrader.core import Ledger


class TestThompsonSampler(unittest.TestCase):
    def test_better_strategy_wins_most(self):
        # trend 历史全胜、pullback 全败 → 采样压倒性偏向 trend
        stats = {"trend": {"alpha": 50, "beta": 0},
                 "pullback": {"alpha": 0, "beta": 50},
                 "hold": {"alpha": 0, "beta": 0}}
        rng = random.Random(1)
        picks = [ST.ThompsonSampler.choose(stats, rng) for _ in range(100)]
        self.assertGreater(picks.count("trend"), 90)

    def test_cold_start_explores(self):
        # 三套都无历史 → 各有机会被选中（探索）
        stats = {s: {"alpha": 0, "beta": 0} for s in ST.STRATEGIES}
        rng = random.Random(42)
        picks = [ST.ThompsonSampler.choose(stats, rng) for _ in range(300)]
        self.assertEqual(len(set(picks)), 3, "冷启动应三套都试过")

    def test_sample_in_unit_interval(self):
        rng = random.Random(0)
        for _ in range(50):
            v = ST.ThompsonSampler.sample(3, 7, rng)
            self.assertGreaterEqual(v, 0)
            self.assertLessEqual(v, 1)


class TestStrategyLearning(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.gettempdir()) / f"pt_st_{id(self)}.db"
        if self.db.exists():
            self.db.unlink()

    def tearDown(self):
        if self.db.exists():
            self.db.unlink()

    def test_feedback_updates_beta(self):
        led = Ledger(self.db)
        led.init_strategies(ST.STRATEGIES)
        led.update_strategy("trend", win=True)
        led.update_strategy("trend", win=True)
        led.update_strategy("pullback", win=False)
        s = led.strategy_stats()
        self.assertEqual(s["trend"], {"alpha": 2, "beta": 0})
        self.assertEqual(s["pullback"], {"alpha": 0, "beta": 1})
        led.close()

    def test_choices_recorded(self):
        sim = Simulator(str(self.db), prefer_real=False, seed=7)
        sim.run(sim.dates()[-40:])
        hist = sim.led.strategy_history()
        self.assertEqual(len(hist), 40)
        self.assertTrue(all(h["strategy"] in ST.STRATEGIES for h in hist))
        sim.led.close()

    def test_decisions_tagged_with_strategy(self):
        sim = Simulator(str(self.db), prefer_real=False, seed=3)
        out = sim.run(sim.dates()[-20:])
        self.assertTrue(all("strategy" in x["decision"] for x in out))
        sim.led.close()

    def test_reproducible_with_seed(self):
        db2 = Path(tempfile.gettempdir()) / f"pt_st2_{id(self)}.db"
        if db2.exists():
            db2.unlink()
        s1 = Simulator(str(self.db), prefer_real=False, seed=99)
        s2 = Simulator(str(db2), prefer_real=False, seed=99)
        p1 = [x["decision"]["strategy"] for x in s1.run(s1.dates()[-30:])]
        p2 = [x["decision"]["strategy"] for x in s2.run(s2.dates()[-30:])]
        self.assertEqual(p1, p2, "同种子同行情应得到相同策略序列")
        s1.led.close(); s2.led.close()
        for p in (db2,):
            if p.exists(): p.unlink()

    def test_strategy_can_be_disabled(self):
        sim = Simulator(str(self.db), prefer_real=False, use_strategy=False)
        out = sim.run(sim.dates()[-20:])
        hist = sim.led.strategy_history()
        self.assertEqual(len(hist), 0, "关闭策略学习后不应记选择")
        sim.led.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
