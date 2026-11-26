"""Isolated external-host test through HTTP, with an optional persistent panel preview."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import threading
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from papertrader import market
from papertrader.service import AccountService, make_server, TZ


def run(csv_path, days, hold_days, directory, port=0):
    rows = market.read_snapshot(csv_path) if csv_path else market.synthetic(n=120, end="2025-12-31")
    dates = market.market_dates(rows)
    if days < 2 or len(dates) < days + 20 or hold_days < 1:
        raise ValueError("Need at least 20 warm-up dates and positive holding period")
    tested = dates[-days:]
    service = AccountService(directory, sandbox=True, csv_path=csv_path)
    server = make_server(service, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(path, data=None):
        request = Request(base + path, headers={"Authorization": "Bearer " + service.token,
                                               "Content-Type": "application/json"},
                          data=json.dumps(data).encode() if data is not None else None)
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    entered, checked, event_cursor, observed_feedback = {}, 0, 0, []
    entry_records, attribution = {}, []
    expected_stats = {arm: {"alpha": 0, "beta": 0} for arm in ("trend", "pullback")}
    try:
        for index, date in enumerate(tested):
            # The test controller advances the clock. The separate host below
            # only receives public HTTP context, never the future rows.
            with service.sim.led._lock:
                service.sim.market = [row for row in rows if row["date"] <= date]
                service.sim.source = "provided_csv" if csv_path else "synthetic"
                service.now = lambda d=date: datetime.fromisoformat(d + "T20:00:00").replace(tzinfo=TZ)
            task = call("/api/context")
            positions = task["observation"]["positions"]
            mature = next((p["code"] for p in positions if index - entered.get(p["code"], index) >= hold_days), None)
            if mature:
                proposal = {"action": "sell", "code": mature, "weight": 0,
                            "reason": "按隔离测试宿主的计划提交该决策。"}
            elif task["allowed_actions"]["buy"] and len(positions) < 2:
                # Deliberately different from the built-in first-candidate rule.
                proposal = {"action": "buy", "code": task["allowed_actions"]["buy"][-1], "weight": .05,
                            "reason": "授权范围内出现合适机会，先以 5% 试探仓位买入。"}
            else:
                proposal = {"action": "hold", "code": "", "weight": 0,
                            "reason": "暂无合适机会，等待买入信号或按计划离场。"}
            result = call("/api/submit", {"task_id": task["task_id"], "proposal": proposal})
            repeated = call("/api/submit", {"task_id": task["task_id"], "proposal": proposal})
            assert repeated["id"] == result["id"], "Retry created another decision"
            if result["outcome"].get("status") == "filled":
                if proposal["action"] == "buy":
                    entered[proposal["code"]] = index
                    entry_records[proposal["code"]] = {
                        "decision_id": result["id"], "entry_id": result["outcome"]["entry_id"],
                        "arm": result["policy"]["arm"], "brain": result["proposal"]["brain"]}
                elif proposal["action"] == "sell":
                    entered.pop(proposal["code"], None)
                    original = entry_records.pop(proposal["code"])
                    feedback = result["outcome"]["feedback"]
                    for field in ("decision_id", "entry_id", "arm", "brain"):
                        assert feedback[field] == original[field], f"Incorrect attribution: {field}"
                    assert feedback["close_id"] == result["outcome"]["trade_id"]
                    assert feedback["before"] == expected_stats[original["arm"]]
                    win = result["outcome"]["pnl_pct"] > 0
                    assert feedback["win"] == win
                    expected_stats[original["arm"]]["alpha" if win else "beta"] += 1
                    assert feedback["after"] == expected_stats[original["arm"]]
                    attribution.append({**original, "close_id": feedback["close_id"],
                                        "exit_arm": result["policy"]["arm"], "win": win,
                                        "pnl_pct": result["outcome"]["pnl_pct"], "verified": True})
            assert call("/api/state")["strategy"] == expected_stats, "Wrong arm's statistics changed"
            snapshot = call("/api/recheck", {"decision_id": result["id"]})
            assert snapshot["matches_recorded"], "Saved gate differs from recalculation"
            checked += 1
            page = call("/api/events?since=" + str(event_cursor))
            for event in page["items"]:
                assert event["id"] > event_cursor
                event_cursor = event["id"]  # An actual host advances after successful memory persistence.
                if event["outcome"].get("feedback"):
                    observed_feedback.append(event["outcome"]["feedback"])
        state = call("/api/state")
        counts = Counter(record["outcome"]["status"] for record in state["decisions"])
        actions = Counter(record["proposal"]["action"] for record in state["decisions"])
        learned = sum(c["alpha"] + c["beta"] for c in state["strategy"].values())
        assert learned == len(observed_feedback), "Learning updates differ from settled feedback"
        assert state["chain_ok"] and checked == days
        report = {"agent_source": "deterministic external HTTP host, no LLM calls", "source": state["data_source"],
                  "start": tested[0], "end": tested[-1], "days": days, "hold_sessions": hold_days,
                  "actions": dict(actions), "outcomes": dict(counts), "return_pct": state["return_pct"],
                  "open_positions": len(state["positions"]), "settled_feedback": len(observed_feedback),
                  "strategy": state["strategy"], "rechecked_decisions": checked,
                  "repeated_submissions": checked, "chain_ok": state["chain_ok"],
                  "attribution_checked": len(attribution), "attribution": attribution,
                  "execution": state["execution"], "fee_bps_per_side": state["fee_bps_per_side"]}
        return service, server, thread, report
    except BaseException:
        server.shutdown()
        server.server_close()
        thread.join()
        service.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--days", type=int, default=40)
    parser.add_argument("--hold-days", type=int, default=6)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--preview-port", type=int, default=0)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="papertrader_external_check_") as directory:
        service, server, thread, report = run(args.csv, args.days, args.hold_days, directory, args.preview_port)
        try:
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            if args.preview_port:
                print(f"Preview: http://127.0.0.1:{server.server_port}/#token={service.token}", flush=True)
                print("Historical test account. Stop this process to discard it.", flush=True)
                threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            service.close()


if __name__ == "__main__":
    main()
