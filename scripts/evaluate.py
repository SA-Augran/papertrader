"""Offline evaluation: delayed-feedback bandit and paired account trajectories."""
import argparse
from collections import Counter, deque
from hashlib import sha256
import json
from pathlib import Path
import random
from statistics import mean, median
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from papertrader import Simulator, analytics, market, strategy

ARMS = strategy.ENTRY_ARMS
TRUE_RATES = dict(zip(ARMS, (0.70, 0.35)))


def delayed_trial(selector, seed, rounds=240, delay=5):
    # Generate outcomes by (round, arm) before assignment so both policies see the same environment.
    environment = random.Random(f"rewards:{seed}")
    tape = [{arm: environment.random() < TRUE_RATES[arm] for arm in ARMS} for _ in range(rounds)]
    rng = random.Random(f"policy:{seed}")
    stats = {arm: {"alpha": 0, "beta": 0} for arm in ARMS}
    pending = deque()
    picks = Counter()
    tail_picks = Counter()
    regret = 0
    for step, rewards in enumerate(tape):
        while pending and pending[0][0] <= step:
            _, arm, won = pending.popleft()
            stats[arm]["alpha" if won else "beta"] += 1
        arm, _ = strategy.ThompsonSampler.select(stats, ARMS, rng, selector)
        pending.append((step + delay, arm, rewards[arm]))
        picks[arm] += 1
        if step >= rounds // 2:
            tail_picks[arm] += 1
        regret += max(TRUE_RATES.values()) - TRUE_RATES[arm]
    best = max(TRUE_RATES, key=TRUE_RATES.get)
    return {"best_arm_rate": picks[best] / rounds,
            "best_arm_rate_second_half": tail_picks[best] / sum(tail_picks.values()),
            "expected_regret": regret, "unobserved_at_end": len(pending)}


def mean_metrics(rows):
    return {key: round(mean(row[key] for row in rows), 4) for key in rows[0]}


def account_trial(rows, selector, seed, db, fee_bps, days):
    sim = Simulator(db, offline=True, market_data=rows, selector=selector, seed=seed,
                    execution="next_close", fee_bps=fee_bps)
    try:
        tested_dates = sim.dates()[-days:]
        for date in tested_dates:
            sim.advance(date, auto_adapt=False)
        records = sim.led.decision_history(limit=len(tested_dates))
        following = dict(zip(tested_dates, tested_dates[1:]))
        quotes = {(row["date"], row["code"]): row["close"] for row in rows}
        both = Counter()
        outcomes = Counter()
        rejections = Counter()
        violations = Counter()
        for record in records:
            outcome = record["outcome"] or {"status": "pending"}
            policy = record["policy"]
            outcomes[outcome["status"]] += 1
            if len(policy["draws"]) == len(ARMS):
                both[policy["arm"]] += 1
            if outcome["status"] == "rejected":
                rejections[outcome["gate"]] += 1
            if outcome["status"] == "filled":
                if outcome["date"] != following.get(record["date"]):
                    violations["wrong_fill_date"] += 1
                if outcome["price"] != quotes.get((outcome["date"], outcome["code"])):
                    violations["wrong_fill_price"] += 1
                if outcome["action"] == "buy" and outcome["code"] not in policy["allowed_buys"]:
                    violations["unauthorized_entry"] += 1
        if not sim.led.verify_chain()[0]:
            violations["ledger_chain"] += 1
        overview = sim.overview()
        nav = overview["nav"]
        risk = analytics.metrics(nav)
        return {"selector": selector, "seed": seed, "start": tested_dates[0], "end": tested_dates[-1],
                "dates": len(tested_dates), "outcomes": dict(outcomes), "rejections": dict(rejections),
                "invariant_violations": dict(violations), "both_eligible_picks": dict(both),
                "both_eligible_rounds": sum(both.values()), "posterior_counts": sim.led.verified_strategy_stats(),
                "open_positions": len(overview["positions"]), "return_pct": overview["return_pct"],
                "max_drawdown_pct": risk["max_drawdown_pct"]}
    finally:
        sim.led.close()


def evaluate(rows, source, bandit_seeds, account_seeds, days, fee_bps):
    selectors = ("thompson", "uniform")
    known_rates = {selector: mean_metrics([delayed_trial(selector, seed) for seed in range(bandit_seeds)])
                   for selector in selectors}
    pairs = []
    with TemporaryDirectory(prefix="papertrader_eval_") as tmp:
        for seed in range(account_seeds):
            pair = {}
            for selector in selectors:
                pair[selector] = account_trial(rows, selector, seed,
                                               Path(tmp) / f"{selector}_{seed}.db", fee_bps, days)
            pairs.append(pair)
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    deltas = [pair["thompson"]["return_pct"] - pair["uniform"]["return_pct"] for pair in pairs]
    return {
        "schema_version": 1,
        "configuration": {"data_source": source, "snapshot_sha256": sha256(encoded).hexdigest(),
                          "execution": "next_close", "fee_bps_per_side": fee_bps,
                          "proposer": "local", "fixed_discipline": True, "requested_days": days},
        "known_reward_experiment": {"seeds": bandit_seeds, "rounds": 240, "delay_in_rounds": 5,
                                    "true_rates": TRUE_RATES, "results": known_rates},
        "account_experiment": {"seeds": account_seeds, "paired_return_delta_pct_points":
                               {"median": round(median(deltas), 4),
                                "min": round(min(deltas), 4), "max": round(max(deltas), 4)},
                               "pairs": pairs},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, help="冻结的日线 CSV，字段 date,code,close，可带 name")
    parser.add_argument("--days", type=int, default=240)
    parser.add_argument("--bandit-seeds", type=int, default=32)
    parser.add_argument("--account-seeds", type=int, default=4)
    parser.add_argument("--fee-bps", type=float, default=10)
    parser.add_argument("--output", type=Path, help="保存 JSON 报告")
    args = parser.parse_args()
    if args.days < 2 or args.bandit_seeds < 1 or args.account_seeds < 1:
        parser.error("交易日至少 2 天，两组种子数至少为 1")
    try:
        rows = market.read_snapshot(args.csv) if args.csv else market.synthetic(n=270, end="2026-11-17")
        if len(market.market_dates(rows)) < 2:
            raise ValueError("行情至少需要两个交易日")
        report = evaluate(rows, "provided_csv" if args.csv else "synthetic_fixed",
                          args.bandit_seeds, args.account_seeds, args.days, args.fee_bps)
    except (OSError, ValueError, KeyError, TypeError) as err:
        parser.error(str(err))
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")
    pairs = report["account_experiment"]["pairs"]
    return int(any(run["invariant_violations"] for pair in pairs for run in pair.values()))


if __name__ == "__main__":
    raise SystemExit(main())
