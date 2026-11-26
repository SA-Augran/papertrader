# -*- coding: utf-8 -*-
"""对照实验：固定规则 vs 自适应，唯一变量是规则是否随教训调整。

口径：两组各自独立决策（各自 brain 读各自的规则），面对**同一段市场行情**。
自适应组会从教训里收紧/放宽规则，并把新规则喂回它自己的决策 prompt（discipline_line）
和闸门；固定组永远用种子规则。差异因此体现在决策行为与净值上。

为什么不用"重放同一串决策"：那样两组闸门读的规则虽不同，但决策 weight 若本就没触上限，
结果完全相同，测不出差异——所以改为各自决策。代价是两组决策有各自的随机性，
但本地兜底是确定性的（同行情同状态同决策），在这个设置里仍是可复现的对照。

    python scripts/experiment.py
输出 JSON 对照摘要，可直接贴进报告。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from papertrader import Simulator, analytics, market


def run_experiment(db_a, db_b, days=250):
    rows = market.synthetic(n=days + 30, end="2026-11-17")
    options = {"market_data": rows, "offline": True, "seed": 42, "execution": "next_close", "fee_bps": 10}
    # --- A 组：自适应（各自决策） ---
    a = Simulator(db_a, **options)
    market_days = a.dates()[-days:]
    for d in market_days:
        a.advance(d)          # decide → execute → nav → adapt（自适应）
    nav_a = a.led.nav_series()
    rules_a = a.led.active_rules()
    evo_a = a.led.rule_history()
    a.led.close()

    # --- B 组：固定规则（各自决策，关掉自适应） ---
    b = Simulator(db_b, **options)
    for d in market_days:
        b.advance(d, auto_adapt=False)   # 决策/闸门永远读种子规则
    nav_b = b.led.nav_series()
    rules_b = b.led.active_rules()
    b.led.close()

    cmp = analytics.compare_runs(nav_b, nav_a, "固定规则", "自适应成长")
    return {
        "design": "同一段行情、两组各自独立决策；唯一变量=规则是否随教训自适应并喂回决策",
        "days": len(market_days),
        "final_rules": {"fixed": rules_b, "adaptive": rules_a},
        "adapt_count": len(evo_a),
        "rule_evolution": analytics.rule_evolution(evo_a),
        "comparison": cmp,
        "metrics_fixed": analytics.metrics(nav_b),
        "metrics_adaptive": analytics.metrics(nav_a),
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="papertrader_discipline_") as tmp:
        result = run_experiment(Path(tmp) / "adaptive.db", Path(tmp) / "fixed.db")
        print(json.dumps(result, ensure_ascii=False, indent=2))
