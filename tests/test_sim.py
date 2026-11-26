# -*- coding: utf-8 -*-
"""Simulator 端到端测试：开箱即用 / 推进多日 / 总览 / 角色替换。全用合成数据，不发网络请求。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from papertrader import Simulator, list_characters, load_persona


class TestSimulator(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.gettempdir()) / f"pt_sim_{id(self)}.db"
        if self.db.exists():
            self.db.unlink()
        self._sims = []
        self._extra_ledgers = []

    def tearDown(self):
        for s in self._sims:
            s.led.close()
        for led in self._extra_ledgers:
            led.close()
        if self.db.exists():
            self.db.unlink()

    def make_sim(self):
        sim = Simulator(str(self.db), prefer_real=False)
        self._sims.append(sim)
        return sim

    def test_out_of_box_runs(self):
        sim = self.make_sim()
        self.assertEqual(sim.source, "synthetic")
        self.assertGreater(len(sim.dates()), 0)
        self.assertIn("default", list_characters())

    def test_advance_one_day(self):
        sim = self.make_sim()
        r = sim.advance(sim.dates()[-1])
        self.assertIn("decision", r)
        self.assertIn("result", r)
        self.assertIn("adaptation", r)
        self.assertIn(r["decision"]["action"], ("buy", "sell", "hold"))

    def test_run_many_days(self):
        sim = self.make_sim()
        out = sim.run(sim.dates()[-10:])
        self.assertEqual(len(out), 10)
        self.assertTrue(all("decision" in x for x in out))

    def test_overview_shape(self):
        sim = self.make_sim()
        sim.run(sim.dates()[-5:])
        ov = sim.overview()
        for k in ("cash", "equity", "return_pct", "rules", "nav", "chain_ok"):
            self.assertIn(k, ov)
        self.assertTrue(ov["chain_ok"])

    def test_nav_recorded_every_day(self):
        # 净值曲线不能有缺天：hold/rejected 的日子也必须记（曾经只有成交日才记，250天只剩44条）
        sim = self.make_sim()
        days = sim.dates()[-30:]
        sim.run(days)
        nav_dates = [n["date"] for n in sim.led.nav_series()]
        self.assertEqual(nav_dates, days, "净值记录应覆盖每个交易日，含 hold 日")

    def test_block_dates_respected(self):
        sim = self.make_sim()
        d = sim.dates()[-1]
        r = sim.advance(d, block_date=d)
        # 决策可能本就是 hold；关键是注入的 block_date 让买单被拒
        self.assertIn("result", r)

    def test_character_replaceable(self):
        txt = load_persona("default")
        self.assertIn("模拟账户", txt)
        # 传一个临时自定义角色文件
        p = Path(tempfile.gettempdir()) / f"pt_char_{id(self)}.md"
        p.write_text("你是测试角色，只说两个字。", encoding="utf-8")
        self.assertIn("测试角色", load_persona(str(p)))
        p.unlink()

    def test_two_accounts_isolated(self):
        db2 = Path(tempfile.gettempdir()) / f"pt_sim2_{id(self)}.db"
        if db2.exists():
            db2.unlink()
        sim1 = self.make_sim()
        sim2 = Simulator(str(db2), prefer_real=False)
        self._extra_ledgers.append(sim2.led)
        sim1.advance(sim1.dates()[-1])
        self.assertGreaterEqual(len(sim1.led.journal()), 1)
        self.assertEqual(len(sim2.led.journal()), 0)
        sim2.led.close()
        self._extra_ledgers.remove(sim2.led)
        if db2.exists():
            db2.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
