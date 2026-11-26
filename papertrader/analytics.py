# -*- coding: utf-8 -*-
"""analytics.py — 从净值序列和流水算专业风险指标（零依赖，纯 Python）。

课上要讲"含金量"，光说收益率不够；夏普/最大回撤/VaR 是基金业绩的通用语言。
全部按日频口径；年化因子 252（A 股交易日数）。样本不足时返回 None，不硬算。
"""
import math

TRADING_DAYS = 252


def _returns(nav):
    """逐日收益率序列。"""
    vals = [float(n["total"]) for n in nav]
    return [(vals[i] / vals[i - 1] - 1) for i in range(1, len(vals)) if vals[i - 1] > 0]


def metrics(nav, risk_free=0.0):
    """输入 nav_series（[{date,total,cash}]），输出指标 dict。"""
    r = _returns(nav)
    out = {
        "days": len(nav),
        "observations": len(r),
        "total_return_pct": None,
        "annual_return_pct": None,
        "annual_vol_pct": None,
        "sharpe": None,
        "max_drawdown_pct": None,
        "var95_daily_pct": None,
        "best_day_pct": None,
        "worst_day_pct": None,
    }
    if not nav:
        return out
    first, last = float(nav[0]["total"]), float(nav[-1]["total"])
    out["total_return_pct"] = round((last / first - 1) * 100, 2) if first else None
    if not r:
        return out
    out["best_day_pct"] = round(max(r) * 100, 2)
    out["worst_day_pct"] = round(min(r) * 100, 2)
    n = len(r)
    if last > 0 and first > 0 and n >= 2:
        years = n / TRADING_DAYS
        out["annual_return_pct"] = round(((last / first) ** (1 / years) - 1) * 100, 2)
    mean = sum(r) / n
    var = sum((x - mean) ** 2 for x in r) / n if n > 1 else 0.0
    std = math.sqrt(var)
    out["annual_vol_pct"] = round(std * math.sqrt(TRADING_DAYS) * 100, 2)
    if std > 0:
        out["sharpe"] = round((mean - risk_free / TRADING_DAYS) / std * math.sqrt(TRADING_DAYS), 3)
    # 最大回撤：历史峰值到谷底的最大跌幅
    peak, mdd = first, 0.0
    for x in nav:
        v = float(x["total"])
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    out["max_drawdown_pct"] = round(mdd * 100, 2)
    # 历史模拟法 VaR95：收益率 5% 分位数（第 ceil(0.05*n) 小），取绝对值为潜在单日损失
    if n >= 4:
        s = sorted(r)
        idx = max(0, math.ceil(0.05 * n) - 1)
        out["var95_daily_pct"] = round(abs(s[idx]) * 100, 2)
    return out


def trade_stats(trades):
    """从 journal（含 result 的 sell 流水）算胜率、盈亏比、期望。"""
    pnls = []
    for t in trades:
        if t.get("action") != "sell":
            continue
        import re
        m = re.search(r"[-+]?\d+\.?\d*", str(t.get("result", "")))
        if m:
            try:
                pnls.append(float(m.group()))
            except Exception:
                pass
    if not pnls:
        return {"closed": 0, "win_rate_pct": None, "profit_factor": None, "expectancy_pct": None}
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_w = sum(wins)
    gross_l = sum(abs(x) for x in losses)
    return {
        "closed": len(pnls),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 1),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
        "expectancy_pct": round(sum(pnls) / len(pnls), 2),
    }


def rule_evolution(rule_history):
    """规则演化轨迹 → 每种参数一条 [date, value] 序列，供画图。"""
    series = {"date": [], "max_weight": [], "first_buy_cap": [], "chase_limit_pct": []}
    for row in rule_history:
        series["date"].append(row["date"])
        for k in ("max_weight", "first_buy_cap", "chase_limit_pct"):
            series[k].append(row[k])
    return series


def compare_runs(nav_a, nav_b, label_a="固定规则", label_b="自适应"):
    """两组净值序列的对照摘要（用于实验章节）。"""
    a, b = metrics(nav_a), metrics(nav_b)
    keys = ("total_return_pct", "annual_return_pct", "sharpe", "max_drawdown_pct", "var95_daily_pct")
    return {
        "labels": [label_a, label_b],
        "rows": [(k, a.get(k), b.get(k)) for k in keys],
    }
