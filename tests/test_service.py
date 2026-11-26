"""Public service regressions using isolated accounts and generated quotes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from papertrader import market
from papertrader.service import AccountService, ServiceError, make_server, TZ


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.directory = Path(self.folder.name)
        self.service = AccountService(self.directory, sandbox=True)
        self.service.refresh()

    def tearDown(self):
        self.service.close()
        self.folder.cleanup()

    def buy(self, task):
        return {"action": "buy", "code": task["allowed_actions"]["buy"][0], "weight": .05, "reason": "try"}

    def test_task_survives_restart_and_identical_submit_is_idempotent(self):
        task = self.service.context()
        proposal = self.buy(task)
        first = self.service.submit(task["task_id"], proposal)
        self.service.close()
        self.service = AccountService(self.directory, sandbox=True)
        self.service.refresh()
        self.assertEqual(self.service.context(), task)
        self.assertEqual(self.service.submit(task["task_id"], proposal), first)
        self.assertEqual(len(self.service.sim.led.journal()), 1)
        state = self.service.state()
        self.assertEqual(state["fills"][0]["id"], first["outcome"]["trade_id"])
        self.assertEqual(state["fills"][0]["action"], "buy")
        self.assertGreater(state["fills"][0]["value"], 0)
        self.assertIn(proposal["code"], state["quotes"])
        self.assertEqual(state["invested"], 100000)
        with self.assertRaises(ServiceError):
            self.service.submit(task["task_id"], {**proposal, "reason": "different"})

    def test_simultaneous_requests_share_one_task_and_one_trade(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            tasks = list(pool.map(lambda _: self.service.context(), range(4)))
        self.assertEqual(len({t["task_id"] for t in tasks}), 1)
        task = tasks[0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.service.submit(task["task_id"], self.buy(task)), range(4)))
        self.assertEqual(len({r["id"] for r in results}), 1)
        self.assertTrue(self.service.state()["chain_ok"])

    def test_one_owner_and_mode_source_are_explicit(self):
        with self.assertRaises(ValueError):
            AccountService(self.directory, sandbox=True)
        self.service.close()
        with self.assertRaises(ValueError):
            AccountService(self.directory, mode="local", sandbox=True)
        self.service = AccountService(self.directory, sandbox=True)

    def test_external_cannot_override_strategy_or_weight_gate(self):
        task = self.service.context()
        other = next(code for code in market.WATCHLIST_CODES if code not in task["allowed_actions"]["buy"])
        result = self.service.submit(task["task_id"], {
            "action": "buy", "code": other, "weight": .05, "reason": "try", "strategy": "invented"})
        self.assertEqual(result["outcome"]["status"], "rejected")
        self.assertEqual(result["outcome"]["gate"], "strategy")
        self.assertEqual(result["proposal"]["strategy"], task["policy"]["arm"])
        self.assertEqual(sum(v["alpha"] + v["beta"] for v in self.service.state()["strategy"].values()), 0)

    def test_commitment_is_checked_at_submit_after_task_creation(self):
        task = self.service.context()
        code = task["allowed_actions"]["buy"][0]
        self.service.commitment(code, "wait today")
        result = self.service.submit(task["task_id"], self.buy(task))
        self.assertEqual(result["outcome"]["gate"], "commitment")
        replay = self.service.sim.recheck(result["id"], ignore_commitment=True)
        self.assertTrue(replay["matches_recorded"])
        self.assertTrue(replay["rechecked"]["allowed"])

    def test_execution_failure_recovers_staged_task(self):
        task = self.service.context()
        proposal = self.buy(task)
        with patch.object(self.service.sim.led, "execute", side_effect=RuntimeError("fault")):
            with self.assertRaises(RuntimeError):
                self.service.submit(task["task_id"], proposal)
        self.assertIsNone(self.service.sim.led.decision_on(task["date"])["outcome"])
        recovered = self.service.submit(task["task_id"], proposal)
        self.assertEqual(recovered["outcome"]["status"], "filled")
        self.assertEqual(len(self.service.sim.led.journal()), 1)

    def test_interrupted_previous_day_must_be_recovered_before_new_context(self):
        task = self.service.context()
        proposal = self.buy(task)
        with patch.object(self.service.sim.led, "execute", side_effect=RuntimeError("fault")):
            with self.assertRaises(RuntimeError):
                self.service.submit(task["task_id"], proposal)
        self.service.sim.market += [{"code": proposal["code"], "date": "2026-01-02", "close": 100}]
        with self.assertRaises(ServiceError):
            self.service.context()
        result = self.service.submit(task["task_id"], proposal)
        self.assertEqual(result["outcome"]["status"], "filled")
        self.assertEqual(self.service.context()["date"], "2026-01-02")

    def test_market_fetch_failure_is_visible_and_never_synthetic(self):
        self.service.close()
        self.service = AccountService(self.directory / "unavailable")
        with self.assertLogs("papertrader.service", level="ERROR"), patch(
                "papertrader.service.subprocess.run", side_effect=TimeoutError("network")):
            self.assertIsNone(self.service.refresh())
        self.assertEqual(self.service.sim.market, [])
        self.assertIn("failed", self.service.state()["market_error"])

    def test_real_mode_rejects_stale_future_and_preclose_quotes(self):
        self.service.close()
        self.service = AccountService(self.directory / "real", now=lambda: datetime(2026, 11, 25, 20, tzinfo=TZ))
        self.service.sim.market = market.synthetic(n=25, end="2026-11-24")
        with self.assertRaises(ServiceError):
            self.service.context()
        self.service.sim.market = market.synthetic(n=25, end="2026-11-25")
        self.assertEqual(self.service.context()["date"], "2026-11-25")
        self.service.now = lambda: datetime(2026, 11, 25, 12, tzinfo=TZ)
        with self.assertRaises(ServiceError):
            self.service.context()

    def test_expired_task_pause_and_missing_market(self):
        task = self.service.context()
        self.service.set_paused(True)
        with self.assertRaises(ServiceError):
            self.service.submit(task["task_id"], self.buy(task))
        self.service.set_paused(False)
        self.service.sim.market = market.synthetic(n=25, end="2026-01-02")
        with self.assertRaises(ServiceError):
            self.service.submit(task["task_id"], self.buy(task))
        self.service.error = "market unavailable"
        with self.assertRaises(ServiceError):
            self.service.context()

    def test_background_is_not_backdated_into_past_tasks(self):
        self.service.now = lambda: datetime(2026, 1, 2, 20, tzinfo=TZ)
        self.service.observation("future news", "host")
        self.assertEqual(self.service.context()["background"], [])

    def test_events_and_learning_are_finalized_only(self):
        task = self.service.context()
        first = self.service.submit(task["task_id"], self.buy(task))
        self.assertEqual(self.service.events()["cursor"], first["id"])
        self.assertEqual(self.service.events(first["id"])["items"], [])
        self.assertEqual(sum(v["alpha"] + v["beta"] for v in self.service.state()["strategy"].values()), 0)
        rows = self.service.sim.market
        code = first["proposal"]["code"]
        later = market._trading_days(1, "2026-01-02")[0]
        price = market.price_of(rows, task["date"], code)
        self.service.sim.market = rows + [{"code": code, "date": later, "close": price * 1.1}]
        second_task = self.service.context()
        closed = self.service.submit(second_task["task_id"], {
            "action": "sell", "code": code, "weight": 0, "reason": "close"})
        self.assertEqual(closed["outcome"]["feedback"]["decision_id"], first["id"])
        self.assertEqual(sum(v["alpha"] + v["beta"] for v in self.service.state()["strategy"].values()), 1)


class HttpTests(unittest.TestCase):
    setUp = ServiceTests.setUp
    tearDown = ServiceTests.tearDown
    buy = ServiceTests.buy
    def test_http_auth_assets_origin_and_bad_input(self):
        server = make_server(self.service, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/") as response:
                html = response.read()
                self.assertIn(b"/app.js", html)
                self.assertIn(b"/vendor/echarts.min.js", html)
                self.assertNotIn(b"https://", html)
                self.assertNotIn(b"onclick=", html)
            for asset, mime in (("/app.css", "text/css"), ("/app.js", "text/javascript"),
                                ("/assets/bg-snow-deep.jpg", "image/jpeg"),
                                ("/vendor/echarts.min.js", "text/javascript")):
                with urlopen(base + asset) as response:
                    self.assertEqual(response.headers.get_content_type(), mime)
                    self.assertGreater(len(response.read()), 100)
            with self.assertRaises(HTTPError) as unauthorized:
                urlopen(base + "/api/state")
            self.assertEqual(unauthorized.exception.code, 401)
            def post(path, data, **headers):
                request = Request(base + path, data=json.dumps(data).encode(), headers={
                    "Authorization": "Bearer " + self.service.token, "Content-Type": "application/json", **headers})
                with urlopen(request) as response:
                    return json.load(response)
            with self.assertRaises(HTTPError) as cross:
                post("/api/pause", {"paused": True}, Origin="https://example.com")
            self.assertEqual(cross.exception.code, 403)
            with self.assertRaises(HTTPError) as bad:
                post("/api/submit", {"task_id": [], "proposal": {}})
            self.assertEqual(bad.exception.code, 400)
            task = self.service.context()
            result = post("/api/submit", {"task_id": task["task_id"], "proposal": self.buy(task)})
            self.assertEqual(result["outcome"]["status"], "filled")
            self.assertNotIn("token", post("/api/pause", {"paused": True}))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
