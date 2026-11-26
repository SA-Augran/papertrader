# -*- coding: utf-8 -*-
"""Brain 链测试：本地兜底 / 教训喂回 / 言行一致 / 无 key 也能决策。
全部走 mock，不发真实 LLM 请求、不装 openai 也能过。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from papertrader.core import Ledger
from papertrader.brain import Brain
from papertrader import market as M


class TestBrain(unittest.TestCase):
    def test_low_win_rate_reduces_size_without_stopping_learning(self):
        with patch.object(self.led, "_pnls", return_value=[-1] * 8):
            proposal = self.brain.local_fallback(
                self.market, self.date, strategy="trend", candidates=["600519"])
        self.assertEqual(proposal["action"], "buy")
        self.assertEqual(proposal["weight"], .05)
        self.assertEqual(proposal["tag"], "trend")

    def setUp(self):
        self.db = Path(tempfile.gettempdir()) / f"pt_brain_{id(self)}.db"
        if self.db.exists():
            self.db.unlink()
        self.led = Ledger(self.db)
        self.led.init_account()
        self.market = M.synthetic(n=60)
        self.date = M.market_dates(self.market)[-1]
        # 显式设成无 provider，保证走本地兜底，不碰环境变量里的 key
        self.brain = Brain(self.led, persona_text="测试角色", primary=None, secondary=None)

    def tearDown(self):
        self.led.close()
        if self.db.exists():
            self.db.unlink()

    def test_no_key_falls_back_local(self):
        d = self.brain.decide(self.market, self.date)
        self.assertIn(d["action"], ("buy", "sell", "hold"))
        self.assertTrue(d["brain"].startswith("local("))

    def test_fallback_returns_valid_shape(self):
        d = self.brain.local_fallback(self.market, self.date)
        for k in ("action", "code", "weight", "reason", "confidence", "tag"):
            self.assertIn(k, d)
        self.assertGreaterEqual(d["weight"], 0)
        self.assertLessEqual(d["weight"], 1)

    def test_fallback_respects_waiting_stance(self):
        self.led.set_stance(self.date, "600519", "观望", days=2)
        d = self.brain.local_fallback(self.market, self.date)
        # 观望立场下不应给出买入
        self.assertNotEqual(d["action"], "buy")

    def test_lessons_empty_when_samples_low(self):
        self.assertEqual(self.brain.lessons_line(), "")

    def test_lessons_appear_after_samples(self):
        # 造低胜率样本（7 亏 3 赚），触发"出手别急"那条教训
        for i in range(10):
            self.led.conn().execute(
                "INSERT INTO trades(date,code,action,reason,result) VALUES(?,?,?,?,?)",
                (f"2026-01-{i+1:02d}", "600519", "sell", "x",
                 "-2.0%" if i < 7 else "+3.0%"))
        self.led.conn().commit()
        self.assertIn("复盘", self.brain.lessons_line())
        self.assertIn("胜率", self.brain.lessons_line())

    def test_extract_json_clamps_fields(self):
        d = self.brain._extract_json(
            '好的 {"action":"buy","code":"600519","weight":9.9,"reason":"test","confidence":5}')
        self.assertEqual(d["action"], "buy")
        self.assertEqual(d["weight"], 1.0)
        self.assertEqual(d["confidence"], 1.0)

    def test_extract_json_invalid_action_to_hold(self):
        d = self.brain._extract_json('{"action":"short","code":"600519","weight":0.1,"reason":"x"}')
        self.assertEqual(d["action"], "hold")

    def test_extract_json_none_on_garbage(self):
        self.assertIsNone(self.brain._extract_json("完全不是 JSON"))

    def test_llm_chain_mock(self):
        # 模拟主模型返回一个合法决策 JSON
        calls = []
        def fake_call(provider, messages, max_tokens, timeout):
            calls.append(provider.get("model", "?"))
            return '{"action":"hold","code":"","weight":0,"reason":"mock","confidence":0.5}'
        self.brain.primary = {"model": "mock-primary", "glm_style": False}
        self.brain._call = fake_call
        text, tag = self.brain._llm([{"role": "user", "content": "x"}])
        self.assertIn("hold", text)
        self.assertEqual(tag, "primary")
        self.assertEqual(calls, ["mock-primary"])

    def test_llm_chain_falls_through_on_exception(self):
        def bad_call(provider, messages, max_tokens, timeout):
            raise RuntimeError("429")
        self.brain.primary = {"model": "mock-primary", "glm_style": False}
        self.brain.secondary = {"model": "mock-secondary", "glm_style": False}
        self.brain._call = bad_call
        text, tag = self.brain._llm([{"role": "user", "content": "x"}])
        self.assertIsNone(text)
        self.assertEqual(tag, "no-llm")


if __name__ == "__main__":
    unittest.main(verbosity=2)
