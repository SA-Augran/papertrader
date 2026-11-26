# -*- coding: utf-8 -*-
"""brain.py — LLM 分层决策 + 零成本聪明兜底 + 策略教训喂回（provider 可插拔）。

链：主模型（默认 GLM 免费档）→ 备模型（默认 DeepSeek）→ 本地规则兜底。
任何一层失败都快速降级，不让单次限流拖死整条链。
配置全走环境变量 / 构造参数，无私人 key、无服务器地址、无 Appwrite。
"""
import json
import os
import random
import re
from copy import deepcopy

from . import market as M
from . import strategy as ST

DEFAULT_SYS = """你经营一个虚拟模拟盘账户（教育用途，非真实交易）。这是你自己的钱、自己的盈亏。
风格：波段偏短线，持有几天到几周，做趋势和回调，不盯分时。
谨慎但好奇，宁可错过不乱动手；每笔操作都要说得出理由；亏了会反思，不上头追涨杀跌。
只输出 JSON：{"action":"buy|sell|hold","code":"6位代码","weight":0到LIMIT,"reason":"一两句口语","confidence":0到1}
{纪律}
只在自选池；weight 是目标仓位占总资产比例；拿不准就 hold（hold 永远合规）；多数时候 hold 合理。"""


class Brain:
    def __init__(self, led, persona_text="", primary=None, secondary=None,
                 use_strategy=True, seed=None, *, offline=False, proposer=None,
                 selector="thompson"):
        self.led = led
        self.persona = persona_text or ""
        self.use_strategy = use_strategy
        self.seed = seed
        self.rng = random.Random(seed)   # 可注入种子 → 决策可复现
        if selector not in ("thompson", "uniform"):
            raise ValueError("selector 必须是 thompson 或 uniform")
        self.selector = selector
        self.proposer = proposer
        self.offline = offline
        # provider 配置：显式参数优先，否则读环境变量，都没有则该层跳过
        self.primary = primary or self._env_provider(
            "PRIMARY", "ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4/", "glm-4.5-flash")
        self.secondary = secondary or self._env_provider(
            "SECONDARY", "DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-chat")

    @staticmethod
    def _env_provider(prefix, key_var, default_base, default_model):
        key = os.environ.get(key_var, "").strip()
        if not key:
            return None
        return {
            "api_key": key,
            "base_url": os.environ.get(f"{prefix}_BASE_URL", default_base).strip(),
            "model": os.environ.get(f"{prefix}_MODEL", default_model).strip(),
            "glm_style": "bigmodel" in default_base,  # glm-4.5-flash 需禁用 thinking
        }

    def _call(self, provider, messages, max_tokens, timeout):
        from openai import OpenAI  # 延迟导入：纯本地/测试不需要装
        extra = {}
        if provider.get("glm_style"):
            extra["extra_body"] = {"thinking": {"type": "disabled"}}
        c = OpenAI(api_key=provider["api_key"], base_url=provider["base_url"],
                   timeout=timeout, max_retries=0)
        r = c.chat.completions.create(
            model=provider["model"], temperature=0.7, max_tokens=max_tokens,
            messages=messages, **extra)
        return (r.choices[0].message.content or "").strip()

    def _llm(self, messages, max_tokens=300, fast=False):
        if self.offline:
            return None, "no-llm"
        for provider, tag in ((self.primary, "primary"), (self.secondary, "secondary")):
            if not provider:
                continue
            try:
                text = self._call(provider, messages, max_tokens, 8.0 if fast else 12.0)
                if text:
                    return text, tag
            except Exception:
                continue
        return None, "no-llm"

    # ---------- 喂给决策的上下文（全部读账本真源） ----------
    def discipline_line(self):
        R = self.led.active_rules()
        return (f"- 单股仓位≤总资产 {R['max_weight']:.0%}；首次建仓≤{R['first_buy_cap']:.0%}；"
                f"单日涨幅超 {R['chase_limit_pct']:.0f}% 不追。这几条是你自己定的，出手前自己把关；"
                f"weight 填 0 到 {R['max_weight']:.2f}。")

    def lessons_line(self):
        pnls = self.led._pnls()
        if len(pnls) < 8:
            return ""
        wins = [x for x in pnls if x > 0]
        losses = [x for x in pnls if x < 0]
        tips, rate = [], len(wins) / len(pnls)
        if rate < 0.4:
            tips.append(f"这段胜率约 {rate:.0%}，出手别急，没把握就空仓等。")
        elif rate > 0.65:
            tips.append(f"这段胜率约 {rate:.0%}，节奏对了就延续，但别因此放大仓位。")
        if wins and losses and (sum(abs(x) for x in losses) / len(losses)) > (sum(wins) / len(wins)):
            tips.append("亏的那几笔比赚的狠，宁可少赚也别让单笔亏太大。")
        if not tips:
            tips.append(f"已复盘 {len(pnls)} 笔，打法大体稳，按既定节奏来。")
        return "\n你自己复盘下来的经验：" + "".join(tips)

    def _extract_json(self, text):
        if not text:
            return None
        m = re.search(r"\{.*?\}", text, re.S)
        if not m:
            return None
        try:
            return self._normalize_proposal(json.loads(m.group()))
        except (ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def _normalize_proposal(d):
        action = str(d.get("action", "hold")).lower()
        return {
            "action": action if action in ("buy", "sell", "hold") else "hold",
            "code": str(d.get("code", "")).strip(),
            "weight": max(0.0, min(float(d.get("weight", 0)), 1.0)),
            "reason": str(d.get("reason") or "")[:200],
            "confidence": max(0.0, min(float(d.get("confidence", 0.5)), 1.0)),
        }

    # ---------- 策略选择（Thompson Sampling） ----------
    def strategy_policy(self, market, date):
        if not self.use_strategy:
            return {"arm": "hold", "draws": {}, "candidates": {}, "allowed_buys": None}
        stats = self.led.verified_strategy_stats()
        candidates = ST.entry_candidates(self.led, market, date, self.led.active_rules())
        eligible = [arm for arm, codes in candidates.items() if codes]
        rng = self.rng if self.seed is None else random.Random(f"{self.seed}:{date}")
        arm, draws = ST.ThompsonSampler.select(stats, eligible, rng, self.selector)
        return {"arm": arm, "draws": draws, "candidates": candidates,
                "allowed_buys": candidates.get(arm, []), "selector": self.selector, "stats": stats}

    # ---------- 决策 ----------
    def decision_snapshot(self, market, date):
        quotes = {}
        for code, name in self.led.watchlist():
            price = M.close_on(market, date, code)
            if price is not None:
                history = M.history_upto(market, date, code, 20)
                quotes[code] = {"name": name, "price": price, "ma5": M.ma(history, 5),
                                "ma20": M.ma(history, 20)}
        return {"date": date, "quotes": quotes, "equity": self.led.equity(market, date),
                "cash": self.led.cash(), "positions": self.led.positions(),
                "rules": self.led.active_rules(), "commitments": self.led.commitments_on(date),
                "stances": self.led.stance_line(date, for_decide=True)}

    def decide(self, market, date, ww_feed=None, *, policy=None, snapshot=None):
        standalone = policy is None
        if policy is None:
            policy = self.strategy_policy(market, date)
        if snapshot is None:
            snapshot = self.decision_snapshot(market, date)
        chosen = policy["arm"]
        allowed = policy["allowed_buys"]
        if self.proposer is not None:
            actions = {"buy": list(allowed) if allowed is not None else list(snapshot["quotes"]),
                       "sell": [p["code"] for p in snapshot["positions"]], "hold": True}
            proposal = self._normalize_proposal(self.proposer(deepcopy(snapshot), actions))
            brain = "custom"
        else:
            proposal, brain = self.model_proposal(snapshot, ww_feed, chosen, allowed)
        if proposal is None:
            proposal = self.local_fallback(market, date, strategy=chosen, candidates=allowed)
            brain = "local(" + proposal.get("tag", "hold") + ")"
        proposal.update(brain=brain, strategy=chosen)
        if self.use_strategy and standalone:
            self.led.record_strategy_choice(date, chosen, proposal["action"], policy["draws"].get(chosen, 0))
        return proposal

    def model_proposal(self, snapshot, ww_feed, arm, allowed):
        if self.offline:
            return None, "no-llm"
        quotes = snapshot["quotes"]
        lines = [f"- {code} {q['name']}: 收{q['price']:.2f} MA5={q['ma5']} MA20={q['ma20']}"
                 for code, q in quotes.items()]
        state = (f"今天是 {snapshot['date']}，总资产 ¥{snapshot['equity']:,.0f}，"
                 f"现金 ¥{snapshot['cash']:,.0f}。\n自选池日线：\n" + "\n".join(lines))
        state += "\n持仓：" + ("、".join(p["code"] for p in snapshot["positions"]) or "无")
        state += snapshot["stances"] + self.lessons_line()
        for commitment in snapshot["commitments"]:
            state += f"\n你承诺在 {commitment['through']} 前（含）不给 {commitment['code']} 开新仓。"
        if ww_feed:
            state += "\n近期冲浪内容（只作参考）：\n" + "\n".join(
                "- " + str(x)[:120] for x in ww_feed[-6:])
        system = (DEFAULT_SYS.replace("{纪律}", self.discipline_line())
                  .replace("0到LIMIT", f"0 到 {snapshot['rules']['max_weight']:.2f}"))
        if allowed is not None:
            label = {"trend": "趋势", "pullback": "回调", "hold": "暂无开仓信号"}[arm]
            candidates = "、".join(allowed) or "无"
            system += (f"\n今天的打法为{label}，买入只可从这些标的选择：{candidates}。"
                       "你可以放弃所有机会，也可提出卖出已有持仓。")
        if self.persona:
            system += "\n\n" + self.persona
        messages = [{"role": "system", "content": system}, {"role": "user", "content": state}]
        text, brain = self._llm(messages)
        return self._extract_json(text), brain

    def narrate(self, entry):
        """只在主动打开复盘时，用已有事实补一段角色口吻。"""
        text, source = self._llm([
            {"role": "system", "content": (self.persona + "\n用第一人称写两句交易复盘。"
                                         "只使用已提供的事实，不添加数字、交易或动机。")},
            {"role": "user", "content": json.dumps(entry, ensure_ascii=False)},
        ], max_tokens=150, fast=True)
        if text:
            entry["commentary"] = text[:300]
            entry["commentary_source"] = source

    # ---------- 零成本聪明兜底 ----------
    def local_fallback(self, market, date, strategy=None, candidates=None):
        R = self.led.active_rules()
        stances = self.led.stance_line(date, for_decide=True)
        waiting = any(k in stances for k in ("观望", "先不动", "看清", "不动手"))
        cautious = False
        pnls = self.led._pnls()
        if len(pnls) >= 8:
            cautious = sum(1 for x in pnls if x > 0) / len(pnls) < 0.4

        # 卖出/减仓是风控，任何打法下都优先（不被 strategy 或观望压制）
        sell = ST.sell_signal(self.led, market, date)
        if sell:
            sell["tag"] = "ma-sell"
            return sell

        # 明确观望立场继续等待，低胜率则缩小试仓，避免空仓后永久停止采样。
        if waiting:
            return {"action": "hold", "code": "", "weight": 0,
                    "reason": "我说过先看清再动，这会儿没到出手的时候，就先空着。",
                    "confidence": 0.5, "tag": "stance-hold"}

        # 买入信号来自被 Thompson 选中的打法（学习闭环的核心）
        if strategy in ST.ENTRY_ARMS:
            if candidates is None:
                candidates = ST.entry_candidates(self.led, market, date, R)[strategy]
            committed = {item["code"] for item in self.led.commitments_on(date)}
            code = next((code for code in candidates if code not in committed), None)
            if code:
                reason = ("趋势向好，我想小仓跟一段。" if strategy == "trend"
                          else "趋势还在，我想试试这个回调。")
                if cautious:
                    reason += "最近胜率偏低，只用5%资金重新观察。"
                return {"action": "buy", "code": code, "weight": min(R["first_buy_cap"], .05) if cautious else R["first_buy_cap"],
                        "reason": reason, "confidence": 0.55, "tag": strategy}
        # 选中的打法当天没有合适标的 → 空仓
        return {"action": "hold", "code": "", "weight": 0,
                "reason": "翻了翻自选，没到非动不可的时候，先这样。",
                "confidence": 0.5, "tag": "hold"}
