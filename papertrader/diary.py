"""Turn recorded facts into brief optional diary entries."""

ARM_LABEL = {"trend": "趋势", "pullback": "回调"}


def diary_entries(ledger, limit):
    if limit <= 0:
        return []
    entries = []
    for decision in ledger.decision_history(limit=max(100, limit)):
        outcome = decision["outcome"] or {}
        feedback = outcome.get("feedback")
        if feedback:
            arm = ARM_LABEL[feedback["arm"]]
            pnl = outcome["pnl_pct"]
            text = (f"{arm}这笔{outcome['code']}平仓了，扣掉模拟成本是 {pnl:+.2f}%。"
                    f"我给{arm}记了一次{'盈利' if feedback['win'] else '未盈利'}样本。")
            entries.append({"type": "feedback", "decision_id": decision["id"],
                            "date": outcome["date"], "text": text, "facts": outcome})
        elif outcome.get("commitment_id"):
            commitment = ledger.commitment(outcome["commitment_id"])
            facts = {"code": commitment["code"], "quote": commitment["quote"],
                     "commitment_id": commitment["id"], "gate": outcome["gate"]}
            entries.append({"type": "commitment_kept", "decision_id": decision["id"],
                            "date": outcome["date"], "facts": facts,
                            "text": f"我提过买入，但答应了先不给{commitment['code']}开新仓，订单没过闸。"})
    for change in ledger.rule_history()[-limit:]:
        entries.append({"type": "rule_change", "date": change["date"], "facts": change,
                        "text": "我把接下来的纪律改了一点。" + change["note"] + "。"})
    return sorted(entries, key=lambda item: item["date"], reverse=True)[:limit]
