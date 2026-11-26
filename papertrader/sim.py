"""Paper account orchestration, optional audit replay, and user-facing reports."""
from math import isfinite

from . import market as M
from .brain import Brain
from .core import Ledger
from .execution import evaluate_order
from .persona import load_persona


class Simulator:
    def __init__(self, db_path, character="default", cache_path=None, prefer_real=True,
                 use_strategy=True, seed=None, *, offline=False, proposer=None, selector="thompson",
                 execution="same_close", fee_bps=0, audit=False, market_data=None):
        if execution not in ("same_close", "next_close"):
            raise ValueError("execution 必须是 same_close 或 next_close")
        if not isfinite(fee_bps) or not 0 <= fee_bps < 10000:
            raise ValueError("fee_bps 必须在 [0, 10000) 内")
        self.led = Ledger(db_path)
        self.led.init_account()
        self.persona_text = load_persona(character)
        self.character = character
        self.cache_path = cache_path
        self.market, self.source = (M.load(cache_path=cache_path, prefer_real=prefer_real and not offline)
                                    if market_data is None else (market_data, "supplied"))
        self.brain = Brain(self.led, persona_text=self.persona_text, use_strategy=use_strategy,
                           seed=seed, offline=offline, proposer=proposer, selector=selector)
        self.execution = execution
        self.fee_bps = fee_bps
        self.audit = audit

    def dates(self):
        return M.market_dates(self.market)

    @staticmethod
    def _deferred(record):
        return (record["policy"]["execution"] == "next_close" and record["submission"]["allowed"]
                and record["proposal"]["action"] in ("buy", "sell"))

    def advance(self, date, ww_feed=None, block_date=None, auto_adapt=True):
        """先结算上一交易日的委托，再根据今天收盘决策。每个日期只有一条决策。"""
        dates = self.dates()
        if date not in dates:
            raise ValueError("该日期在行情中没有交易日")
        with self.led._lock:
            existing = self.led.decision_on(date)
            if existing:
                return self._finish_day(existing, block_date, settlement=None, auto_adapt=False)
            previous = self.led.latest_decision()
            if previous and date < previous["date"]:
                raise ValueError("请按交易日顺序推进")
            settlement = None
            if previous and previous["outcome"] is None:
                if not self._deferred(previous):
                    raise ValueError(f"请先恢复 {previous['date']} 的同日决策")
                settlement = {"decision_id": previous["id"], **self.led.execute(
                    None, self.market, date, block_date=block_date, decision_id=previous["id"])}
            elif previous and previous["outcome"]["date"] == date and self._deferred(previous):
                # 成交已提交，但该交易日可能尚未写入新决策。
                settlement = {"decision_id": previous["id"], **previous["outcome"]}

            policy = self.brain.strategy_policy(self.market, date)
            policy.update(execution=self.execution, fee_bps=self.fee_bps)
            snapshot = self.brain.decision_snapshot(self.market, date)
            proposal = self.brain.decide(self.market, date, ww_feed=ww_feed,
                                         policy=policy, snapshot=snapshot)
            record = self.led.stage_decision(proposal, policy, self.market, date, block_date,
                                             agent_input=snapshot if self.audit else None)
            return self._finish_day(record, block_date, settlement, auto_adapt)

    def _finish_day(self, record, block_date, settlement, auto_adapt):
        date, decision_id = record["date"], record["id"]
        result = record["outcome"]
        if result is None:
            if self._deferred(record):
                self.led.record_nav(self.market, date)
                result = {"executed": False, "status": "pending"}
            else:
                result = self.led.execute(None, self.market, date, block_date=block_date,
                                          decision_id=decision_id)
        adaptation = self.led.adapt_rules(date=date) if auto_adapt else None
        return {"date": date, "decision_id": decision_id, "decision": record["proposal"],
                "result": result, "settlement": settlement, "adaptation": adaptation}

    def run(self, dates=None, ww_feed=None, block_dates=None):
        """从首日或上次保存的位置继续推进，也可传入明确的交易日期。"""
        block_dates = set(block_dates or [])
        if dates is None:
            previous = self.led.latest_decision()
            start = previous["date"] if previous else None
            dates = [date for date in self.dates() if start is None or date >= start]
        return [self.advance(date, ww_feed=ww_feed, block_date=date if date in block_dates else None)
                for date in dates]

    def recheck(self, decision_id, *, stage="submission", ignore_commitment=False):
        """只在内存重算闸门，可移除匹配的承诺以复核单因素变化。"""
        if stage not in ("submission", "execution"):
            raise ValueError("stage 必须是 submission 或 execution")
        record = self.led.decision(decision_id)
        if not record or not record["audit"] or stage not in record["audit"]:
            raise ValueError("该决策阶段没有复核快照")
        audit = record["audit"]
        if audit["version"] != 1:
            raise ValueError("该快照版本需要对应的复核规则")
        facts = audit[stage]
        expected = record["submission"] if stage == "submission" else audit["execution_check"]
        baseline = evaluate_order(record["proposal"], facts)
        changed = {**facts, "commitment": None} if ignore_commitment else facts
        return {"decision_id": decision_id, "stage": stage, "baseline": baseline,
                "matches_recorded": expected == baseline,
                "rechecked": evaluate_order(record["proposal"], changed)}

    def diary(self, limit=10, *, use_model=False):
        """按需生成少量复盘。结构化事实始终返回，可直接用于前端详情。"""
        from .diary import diary_entries
        entries = diary_entries(self.led, limit)
        if use_model and entries:
            self.brain.narrate(entries[0])
        return entries

    def overview(self):
        nav = self.led.nav_series()
        last_total = nav[-1]["total"] if nav else self.led.cash()
        return {
            "character": self.character,
            "data_source": self.source,
            "cash": round(self.led.cash(), 2),
            "equity": round(last_total, 2),
            "return_pct": round((last_total / self.led.invest_base() - 1) * 100, 2),
            "positions": self.led.positions(),
            "rules": self.led.active_rules(),
            "nav": nav,
            "trades": len(self.led.journal(limit=100000)),
            "chain_ok": self.led.verify_chain()[0],
        }

    def report(self):
        """账户、历史风险、规则演化和已验证的策略后验。"""
        from . import analytics
        rep = self.overview()
        rep["metrics"] = analytics.metrics(rep["nav"])
        rep["trade_stats"] = analytics.trade_stats(self.led.journal(limit=100000))
        rep["rule_evolution"] = analytics.rule_evolution(self.led.rule_history())
        rep["strategy"] = {"stats": self.led.verified_strategy_stats(),
                           "history": self.led.strategy_history()}
        return rep
