# -*- coding: utf-8 -*-
"""strategy.py — 策略学习与 Thompson Sampling（无外部依赖）。

学习臂是趋势与回调。观望是每一轮都允许的动作。
Beta(1+盈利笔数, 1+非盈利笔数) 仅使用已结算的开仓。
"""
from . import market as M

STRATEGIES = ("trend", "pullback", "hold")
ENTRY_ARMS = STRATEGIES[:2]


class ThompsonSampler:
    """从 Beta(α+1, β+1) 采样。α/β 是历史成功/失败次数（先验 Beta(1,1)=均匀）。"""

    @staticmethod
    def sample(alpha, beta, rng):
        try:
            return rng.betavariate(alpha + 1, beta + 1)
        except Exception:
            # rng 可能是只实现了 random() 的假对象（测试用），退回均匀采样
            return rng.random()

    @staticmethod
    def choose(stats, rng):
        return ThompsonSampler.select(stats, STRATEGIES, rng)[0]

    @staticmethod
    def select(stats, eligible, rng, selector="thompson"):
        """返回获选臂及实际使用的抽样值。均匀基线给每个可选臂独立均匀抽样。"""
        draws = {}
        for arm in eligible:
            counts = stats.get(arm, {})
            draws[arm] = (rng.random() if selector == "uniform" else
                          ThompsonSampler.sample(counts.get("alpha", 0), counts.get("beta", 0), rng))
        return max(draws, key=draws.get) if draws else "hold", draws


def entry_candidates(led, market, date, rules):
    """同一标的在同一天只属于一个开仓臂。仓位和现金仍由执行闸门检查。"""
    result = {arm: [] for arm in ENTRY_ARMS}
    held = {p["code"] for p in led.positions()}
    for code, _ in led.watchlist():
        if code in held or M.close_on(market, date, code) is None:
            continue
        history = M.history_upto(market, date, code, 20)
        if len(history) < 20 or history[-2] <= 0:
            continue
        fast, slow = M.ma(history, 5), M.ma(history, 20)
        change = (history[-1] / history[-2] - 1) * 100
        if fast <= slow or change > rules["chase_limit_pct"]:
            continue
        arm = "trend" if history[-1] >= fast else "pullback"
        result[arm].append(code)
    return result


def sell_signal(led, market, date):
    """减仓信号不属于策略选择——任何打法下都要允许止损/离场。"""
    for code, name in led.watchlist():
        h = M.history_upto(market, date, code, 30)
        if len(h) < 20:
            continue
        ma5, ma20 = M.ma(h, 5), M.ma(h, 20)
        p = next((x for x in led.positions() if x["code"] == code), None)
        if ma5 and ma20 and ma5 < ma20 and p:
            return {"action": "sell", "code": code, "weight": 0,
                    "reason": f"{name}势头转弱，先出来避一避。", "confidence": 0.5}
    return None
