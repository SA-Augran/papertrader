"""Freeze public daily quotes, run an isolated account, and export the existing demo schema."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from papertrader import Simulator, market


def download(code):
    symbol = code + (".SS" if code.startswith("6") else ".SZ")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2y&interval=1d"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=25) as response:
        series = json.load(response)["chart"]["result"][0]
    quotes = series["indicators"]["quote"][0]["close"]
    rows = []
    for timestamp, close in zip(series["timestamp"], quotes):
        date = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
        if close is not None and "2025-07-01" <= date <= "2025-12-31":
            rows.append(dict(date=date, code=code, name=market.NAME_BY_CODE[code], close=round(close, 4)))
    if len(rows) < 80:
        raise ValueError(f"{code}: insufficient historical quotes")
    return rows


def export(rows, days, seed):
    with TemporaryDirectory(prefix="papertrader_demo_") as folder:
        sim = Simulator(Path(folder) / "demo.db", market_data=rows, offline=True,
                        execution="next_close", fee_bps=10, audit=True, seed=seed)
        try:
            dates = sim.dates()[-days:]
            for date in dates:
                sim.advance(date)
            report = sim.report()
            records = sim.led.decision_history(limit=len(dates))
            trades = sim.led.journal(limit=100000)
            prices = {(r["date"], r["code"]): r["close"] for r in rows}
            positions = []
            for p in report["positions"]:
                price = prices.get((dates[-1], p["code"]), p["cost"])
                positions.append({**p, "name": market.NAME_BY_CODE.get(p["code"], p["code"]),
                                  "price": price, "value": p["shares"] * price,
                                  "pnl_pct": (price / p["cost"] - 1) * 100})
            by_trade = {r["outcome"]["trade_id"]: r for r in records if r.get("outcome")}
            items = []
            for i, t in enumerate(trades):
                trade_id = len(trades) - i
                d = by_trade.get(trade_id, {})
                items.append({**t, "id": len(trades) - i, "day": t["date"], "src": "本地策略 · 历史回放",
                              "basis": t["reason"], "confidence": None, "delta": "—",
                              "recall": None, "decision_id": d.get("id"), "signal_date": d.get("date")})
            navs = [{**n, "day": i + 1} for i, n in enumerate(report["nav"])]
            watch = []
            for code in market.WATCHLIST_CODES:
                h = market.history_upto(rows, dates[-1], code, 2)
                if len(h) == 2:
                    watch.append(dict(code=code, name=market.NAME_BY_CODE[code], price=h[-1],
                                      chg=(h[-1] / h[-2] - 1) * 100))
            rules = report["rules"]
            rule_items = [
                {"n": "R1", "text": f"单只股票仓位不超过总资产的 {rules['max_weight']:.0%}。"},
                {"n": "R2", "text": f"首次建仓不超过总资产的 {rules['first_buy_cap']:.0%}。"},
                {"n": "R3", "text": f"单日涨幅超过 {rules['chase_limit_pct']:.0f}% 不追。"},
            ]
            diary = []
            for d in sim.diary(limit=30):
                match = next((t for t in items if t["date"] == d["date"]), None)
                diary.append(dict(d=d["date"], link=f"#{match['id']}" if match else "", text=d["text"]))
            closed = sum(t["action"] == "sell" for t in trades)
            pnls = [float(t["result"].strip("%")) for t in trades if t["action"] == "sell"]
            gains = [p for p in pnls if p > 0]
            losses = [-p for p in pnls if p < 0]
            pl_ratio = (sum(gains) / len(gains)) / (sum(losses) / len(losses)) if gains and losses else None
            bundle = {
                "overview": dict(day=len(dates), date=dates[-1], cash=report["cash"], total=report["equity"],
                                 ret_pct=report["return_pct"], navs=navs, benchmark=[], positions=positions, watch=watch),
                "journal": {"items": items},
                "growth": dict(rules=rule_items, radar=[len(gains) / closed if closed else 0, pl_ratio, 0, 0, 0, 0],
                               diary=diary, advice=[], stats=dict(trades=sum(t["action"] in ("buy", "sell") for t in trades),
                                                                closed=closed, invest_base=100000)),
                "mood": dict(text="历史行情隔离回放。买卖来自本地策略，平仓后才更新经验。",
                             day=len(dates), ret_pct=report["return_pct"]),
                "experiment": dict(start=dates[0], end=dates[-1], days=len(dates), seed=seed,
                                   execution="next_close", fee_bps_per_side=10, proposer="local",
                                   source="Yahoo chart, unadjusted daily close", chain_ok=report["chain_ok"],
                                   report=report, decisions=records),
            }
            assert report["chain_ok"]
            for record in records:
                assert sim.recheck(record["id"])["matches_recorded"]
            return bundle
        finally:
            sim.led.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--seed", type=int, default=31)
    args = parser.parse_args()
    if args.days < 2:
        parser.error("--days must be at least 2")
    if args.download:
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = [r for group in pool.map(download, market.WATCHLIST_CODES) for r in group]
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["date", "code", "name", "close"])
            writer.writeheader()
            writer.writerows(rows)
    rows = market.read_snapshot(args.csv)
    if len(market.market_dates(rows)) < args.days + 20:
        raise ValueError("Need requested days plus 20 warm-up sessions")
    bundle = export(rows, args.days, args.seed)
    bundle["experiment"]["snapshot_sha256"] = sha256(args.csv.read_bytes()).hexdigest()
    bundle["experiment"]["generated_at"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("window.PAPERTRADER_REPLAY = " + json.dumps(bundle, ensure_ascii=False, allow_nan=False) + ";\n",
                           encoding="utf-8")
    args.output.with_suffix(".json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({**{k: bundle["experiment"][k] for k in ("start", "end", "days", "chain_ok")},
                      "closed": bundle["growth"]["stats"]["closed"], "return_pct": bundle["overview"]["ret_pct"]}))


if __name__ == "__main__":
    main()
