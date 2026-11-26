"""Pure order validation shared by execution and optional audit replay."""

from math import isfinite


def evaluate_order(proposal, facts):
    action = proposal.get("action", "hold")
    checks = []

    def result(allowed, gate, reason):
        checks.append(["pass" if allowed else "fail", reason])
        return {"allowed": allowed, "gate": gate, "reason": reason, "checks": checks}

    if action == "hold":
        return result(True, "hold", "hold 无需过闸")
    if action not in ("buy", "sell"):
        return result(False, "action", "未知订单类型")
    if facts["blocked"]:
        return result(False, "blocked_day", "今天说过不动手")
    if not facts["watched"]:
        return result(False, "watchlist", "不在自选池")
    checks.append(["pass", "在自选池内"])
    if action == "buy" and facts["commitment"] and not facts["shares"]:
        return {**result(False, "commitment", "有效承诺禁止对该标的开新仓"),
                "commitment_id": facts["commitment"]["id"]}
    if action == "buy" and (facts["managed_position"] or
                            (facts["allowed_buys"] is not None and facts["shares"])):
        return result(False, "already_held", "本次策略学习只允许新开仓")
    if action == "buy" and facts["allowed_buys"] is not None:
        if proposal.get("code") not in facts["allowed_buys"]:
            return result(False, "strategy", "标的不在本次打法授权范围")
        checks.append(["pass", "通过打法授权"])
    price = facts["price"]
    if price is None or not isfinite(price) or price <= 0:
        return result(False, "quote", "当日无可用行情")
    rules = facts["rules"]
    if action == "buy":
        weight = float(proposal.get("weight", 0))
        if not (0 < weight <= rules["max_weight"]):
            return result(False, "max_weight", f"仓位必须在 0~{rules['max_weight']:.0%} 之间")
        change = facts["daily_change_pct"]
        if change is not None and change > rules["chase_limit_pct"]:
            return result(False, "chase", f"单日涨幅>{rules['chase_limit_pct']:.0f}% 不追")
        delta = weight * facts["equity"] - facts["shares"] * price
        if delta <= 0:
            return result(False, "size", "目标仓位未超过当前持仓")
        if delta * (1 + facts["fee_rate"]) > facts["cash"] * 0.999:
            return result(False, "cash", "现金不足")
        if not facts["shares"] and weight > rules["first_buy_cap"] + 1e-9:
            return result(False, "first_buy_cap", f"首次建仓≤{rules['first_buy_cap']:.0%}")
        checks.extend([["pass", "仓位合规"], ["pass", "未触发追高线"], ["pass", "现金充足"]])
    elif facts["shares"] <= 0:
        return result(False, "position", "无持仓可卖")
    else:
        checks.append(["pass", "持仓可卖"])
    if not (proposal.get("reason") or "").strip():
        return result(False, "reason", "说不清理由的单子不做")
    return result(True, "pass", "订单合规")
