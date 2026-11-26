# -*- coding: utf-8 -*-
"""papertrader — 给 Agent 的一个小事业：自带角色、真实行情、会成长的模拟盘沙盒。

教育用途 · 模拟盘 · 不接券商 · 不动真实资金 · 不给 Agent 敏感权限。

    from papertrader import Simulator
    sim = Simulator("./data/demo.db")            # 开箱即用，自带默认角色 + 确定性行情
    sim.advance(sim.dates()[-1])                  # 推进一个交易日
    print(sim.overview())                          # 看账户、净值、规则演化
"""
from .core import Ledger
from . import market
from .brain import Brain
from .persona import load_persona, list_characters
from .sim import Simulator
from . import analytics
from . import strategy

__version__ = "0.3.0"
__all__ = ["Simulator", "Ledger", "Brain", "market", "analytics", "strategy",
           "load_persona", "list_characters"]
