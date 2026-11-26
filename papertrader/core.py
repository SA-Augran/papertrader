# -*- coding: utf-8 -*-
"""core.py — 账本 + 规则闸门 + 保守自适应 + 立场账本（零 LLM / 零外部依赖）

设计口径（脱敏自 stock-sim/engine.py）：
- 账本主键 = 真实交易日 YYYY-MM-DD；hash 链防篡改，净值是一部 track record，不是能重置的计数。
- 规则参数单一真源 active_rules()：闸门执行 / 决策 prompt / 前端展示都读它，杜绝"说的数≠执行的数"。
- 自适应只从历史盈亏里保守微调，三道锁（样本门槛 / 单次一步 / 硬边界），看不出信号就不动。
- 立场账本：把角色对外表过态的方向结构化存下来，让"说的"和"做的"同源。
- 无任何私人设定、无 Appwrite/QQ/服务器接线——宿主通过显式入参注入。
"""
import hashlib
import json
import re
import sqlite3
import threading
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

TZ8 = timezone(timedelta(hours=8))

# ---------- 种子规则（自适应的起点与回退基线） ----------
SEED_RULES = {
    "max_weight": 0.25,
    "first_buy_cap": 0.15,
    "chase_limit_pct": 9.0,
}
RULE_BOUNDS = {
    "max_weight": (0.20, 0.30, 0.01),
    "first_buy_cap": (0.12, 0.18, 0.01),
    "chase_limit_pct": (6.0, 12.0, 1.0),
}
ADAPT_MIN_CLOSED = 8
WATCHLIST_SEED = [
    ("600519", "贵州茅台"), ("300750", "宁德时代"), ("601318", "中国平安"),
    ("000001", "平安银行"), ("002594", "比亚迪"), ("600036", "招商银行"),
    ("000858", "五粮液"), ("601899", "紫金矿业"),
]
INIT_CASH = 100_000.0
STANCE_DEFAULT_DAYS = {"观望": 1, "先不动": 1, "看多": 5, "看空": 5, "想买": 3, "想卖": 3}

GENESIS_HASH = "0" * 64


def today_str():
    return datetime.now(TZ8).strftime("%Y-%m-%d")


class Ledger:
    """一个独立模拟账户。所有状态在传入的 SQLite 路径里，互不相干。
    典型用法：led = Ledger("./data/demo.db"); led.init_account()
    """

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = None
        self._lock = threading.RLock()
        self._watch = {}
        self.conn()  # 建表
        self.refresh_watch()

    # ---------- DB ----------
    def conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS positions(code TEXT PRIMARY KEY, shares REAL, cost REAL);
            CREATE TABLE IF NOT EXISTS nav(date TEXT PRIMARY KEY, seq INTEGER, total REAL, cash REAL);
            CREATE TABLE IF NOT EXISTS watchlist(code TEXT PRIMARY KEY, name TEXT,
                added_date TEXT, source TEXT DEFAULT 'seed');
            CREATE TABLE IF NOT EXISTS trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT, seq INTEGER, code TEXT, name TEXT, action TEXT,
                price REAL, shares REAL, value REAL,
                reason TEXT, confidence REAL, source TEXT, strategy TEXT,
                ts TEXT DEFAULT (datetime('now','localtime')),
                gates TEXT, result TEXT, prev_hash TEXT, hash TEXT);
            CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT, text TEXT, ts TEXT DEFAULT (datetime('now','localtime')));
            CREATE TABLE IF NOT EXISTS stances(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT, expire_date TEXT,
                target TEXT, direction TEXT, intent TEXT, quote TEXT,
                status TEXT DEFAULT 'active', source TEXT DEFAULT 'chat',
                ts TEXT DEFAULT (datetime('now','localtime')));
            CREATE TABLE IF NOT EXISTS advice(id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT, say TEXT, did TEXT, res TEXT, up INTEGER);
            CREATE TABLE IF NOT EXISTS rule_history(id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT, max_weight REAL, first_buy_cap REAL, chase_limit_pct REAL,
                note TEXT, ts TEXT DEFAULT (datetime('now','localtime')));
            CREATE TABLE IF NOT EXISTS strategy_stats(name TEXT PRIMARY KEY, alpha REAL, beta REAL);
            CREATE TABLE IF NOT EXISTS strategy_history(id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT, strategy TEXT, action TEXT, sampled REAL,
                ts TEXT DEFAULT (datetime('now','localtime')));
            CREATE TABLE IF NOT EXISTS decisions(
                id INTEGER PRIMARY KEY, date TEXT NOT NULL UNIQUE, proposal TEXT NOT NULL,
                policy TEXT NOT NULL, submission TEXT NOT NULL, outcome TEXT, audit TEXT);
            CREATE TABLE IF NOT EXISTS strategy_entries(
                entry_id INTEGER PRIMARY KEY, decision_id INTEGER NOT NULL UNIQUE,
                code TEXT NOT NULL, arm TEXT NOT NULL, brain TEXT NOT NULL,
                close_id INTEGER UNIQUE, pnl_pct REAL);
            CREATE UNIQUE INDEX IF NOT EXISTS strategy_open_code
                ON strategy_entries(code) WHERE close_id IS NULL;
            CREATE TABLE IF NOT EXISTS entry_commitments(
                id INTEGER PRIMARY KEY, code TEXT NOT NULL, starts TEXT NOT NULL,
                through TEXT NOT NULL, quote TEXT NOT NULL, revised_on TEXT, revision TEXT);
            """)
            self._conn.commit()
        return self._conn

    def meta_get(self, k, d=None):
        r = self.conn().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else d

    def meta_set(self, k, v, commit=True):
        self.conn().execute(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
        if commit:
            self.conn().commit()

    # ---------- 初始化 ----------
    def init_account(self):
        if self.meta_get("init") != "1":
            with self._lock:
                self.conn().execute("INSERT OR REPLACE INTO positions VALUES('CASH',?,1)", (INIT_CASH,))
                for code, name in WATCHLIST_SEED:
                    self.conn().execute("INSERT OR IGNORE INTO watchlist VALUES(?,?,?,?)",
                                        (code, name, today_str(), "seed"))
                self.meta_set("init", "1")
                self.meta_set("invest_base", str(INIT_CASH))
                self.meta_set("seq", "0")
                self.conn().commit()
        self.refresh_watch()

    def watchlist(self):
        return [(r["code"], r["name"]) for r in
                self.conn().execute("SELECT code,name FROM watchlist ORDER BY rowid").fetchall()]

    def refresh_watch(self):
        self._watch = dict(self.watchlist())
        return self._watch

    # ---------- 账户视图 ----------
    def positions(self):
        return [dict(r) for r in self.conn().execute(
            "SELECT * FROM positions WHERE code!='CASH'").fetchall()]

    def cash(self):
        r = self.conn().execute("SELECT shares FROM positions WHERE code='CASH'").fetchone()
        return r["shares"] if r else INIT_CASH

    def invest_base(self):
        return float(self.meta_get("invest_base", str(INIT_CASH)))

    def equity(self, market, date):
        from .market import price_of
        total = self.cash()
        for p in self.positions():
            total += p["shares"] * (price_of(market, date, p["code"]) or p["cost"])
        return total

    # ---------- 规则单一真源 ----------
    def active_rules(self):
        out = dict(SEED_RULES)
        raw = self.meta_get("active_rules", "")
        if raw:
            try:
                saved = json.loads(raw)
                for k in out:
                    lo, hi, _ = RULE_BOUNDS[k]
                    v = saved.get(k)
                    if isinstance(v, (int, float)) and lo <= v <= hi:
                        out[k] = v
            except Exception:
                pass
        return out

    def _pnls(self):
        out = []
        for r in self.conn().execute("SELECT action,result FROM trades ORDER BY id").fetchall():
            if r["action"] != "sell":
                continue
            m = re.search(r"[-+]?\d+\.?\d*", str(r["result"]))
            if m:
                try:
                    out.append(float(m.group()))
                except Exception:
                    pass
        return out

    ADAPT_WINDOW = 12  # 信号只看最近 12 笔平仓，让规则能随近期表现回升（全历史累计会永久卡边界）

    def adapt_rules(self, date=None):
        """同一批平仓只触发一次调参，游标与规则在同一事务中提交。"""
        with self._lock, self.conn():
            closed_id = self.conn().execute(
                "SELECT COALESCE(MAX(id),0) FROM trades WHERE action='sell'").fetchone()[0]
            if closed_id <= int(self.meta_get("rules_closed_id", "0")):
                return {"changed": False, "rules": self.active_rules(), "note": "没有新的平仓样本，维持纪律"}
            result = self._adapt_rules(date)
            self.meta_set("rules_closed_id", closed_id, commit=False)
            return result

    def _adapt_rules(self, date=None):
        """保守自适应。三道锁：样本门槛 8 / 单次一步 / 硬边界。
        信号用滚动窗口（最近 12 笔）而非全历史：全历史亏损率一旦过半就永远过半，
        规则会一路收紧到边界卡死、再也好不回来——这是实测抓到的死锁，故改窗口。"""
        self._adapt_date = date or today_str()
        rules = self.active_rules()
        pnls = self._pnls()
        if len(pnls) < ADAPT_MIN_CLOSED:
            return {"changed": False, "rules": rules,
                    "note": f"样本{len(pnls)}<{ADAPT_MIN_CLOSED}，维持种子"}
        before = dict(rules)
        win = pnls[-self.ADAPT_WINDOW:]
        losses = [x for x in win if x < 0]
        loss_rate = len(losses) / len(win)
        if loss_rate > 0.5:
            rules["chase_limit_pct"] = max(RULE_BOUNDS["chase_limit_pct"][0],
                rules["chase_limit_pct"] - RULE_BOUNDS["chase_limit_pct"][2])
            rules["first_buy_cap"] = max(RULE_BOUNDS["first_buy_cap"][0],
                rules["first_buy_cap"] - RULE_BOUNDS["first_buy_cap"][2])
            note = f"近{len(win)}笔亏损率{loss_rate:.0%}偏高，收紧追高线/首仓"
        elif loss_rate < 0.3:
            rules["chase_limit_pct"] = min(RULE_BOUNDS["chase_limit_pct"][1],
                rules["chase_limit_pct"] + RULE_BOUNDS["chase_limit_pct"][2])
            rules["first_buy_cap"] = min(RULE_BOUNDS["first_buy_cap"][1],
                rules["first_buy_cap"] + RULE_BOUNDS["first_buy_cap"][2])
            note = f"近{len(win)}笔亏损率{loss_rate:.0%}较低，放宽追高线/首仓"
        else:
            note = "盈亏平稳，不动规则"
        streak = worst = 0
        for x in win:
            streak = streak + 1 if x < 0 else 0
            worst = max(worst, streak)
        if worst >= 2:
            rules["max_weight"] = max(RULE_BOUNDS["max_weight"][0],
                rules["max_weight"] - RULE_BOUNDS["max_weight"][2])
            note += "；近窗连亏收紧单股上限"
        # 浮点累加会产生 0.22999999999999998 这种脏值，归整后再比较/存储
        for k in ("max_weight", "first_buy_cap"):
            rules[k] = round(rules[k], 4)
        rules["chase_limit_pct"] = round(rules["chase_limit_pct"], 2)
        # 到边界还按同方向推 = 没真的变化，不算 changed（避免每天记一笔"调整"其实没动）
        changed = any(abs(rules[k] - before[k]) > 1e-9 for k in rules)
        if changed:
            self.meta_set("active_rules", json.dumps(rules), commit=False)
            self.conn().execute(
                "INSERT INTO rule_history(date,max_weight,first_buy_cap,chase_limit_pct,note) "
                "VALUES(?,?,?,?,?)",
                (self._adapt_date, rules["max_weight"], rules["first_buy_cap"],
                 rules["chase_limit_pct"], note))
        else:
            note = "已到边界或信号平稳，维持" if note != "盈亏平稳，不动规则" else note
        return {"changed": changed, "rules": rules, "note": note}

    def rule_history(self):
        """规则演化轨迹：每次自适应调整一条，用于画成长曲线。"""
        return [dict(r) for r in self.conn().execute(
            "SELECT date,max_weight,first_buy_cap,chase_limit_pct,note FROM rule_history ORDER BY id").fetchall()]

    # ---------- 策略学习（Thompson Sampling 的后验与选择历史） ----------
    def init_strategies(self, names):
        for name in names:
            self.conn().execute(
                "INSERT OR IGNORE INTO strategy_stats(name,alpha,beta) VALUES(?,0,0)", (name,))
        self.conn().commit()

    def strategy_stats(self):
        # 历史计数保留给使用低层统计 API 的调用方。策略选择只读已验证的归属。
        return {r["name"]: {"alpha": r["alpha"], "beta": r["beta"]}
                for r in self.conn().execute("SELECT name,alpha,beta FROM strategy_stats")}

    def verified_strategy_stats(self):
        from .strategy import ENTRY_ARMS
        totals = {arm: {"alpha": 0, "beta": 0} for arm in ENTRY_ARMS}
        for row in self.conn().execute(
                "SELECT arm,SUM(pnl_pct>0) AS wins,COUNT(*) AS n "
                "FROM strategy_entries WHERE close_id IS NOT NULL GROUP BY arm"):
            totals[row["arm"]] = {"alpha": row["wins"], "beta": row["n"] - row["wins"]}
        return totals

    def update_strategy(self, name, win):
        col = "alpha" if win else "beta"
        self.conn().execute(f"UPDATE strategy_stats SET {col}={col}+1 WHERE name=?", (name,))
        self.conn().commit()

    def record_strategy_choice(self, date, strategy, action, sampled):
        self.conn().execute(
            "INSERT INTO strategy_history(date,strategy,action,sampled) VALUES(?,?,?,?)",
            (date, strategy, action, sampled))
        self.conn().commit()

    def strategy_history(self):
        return [dict(r) for r in self.conn().execute(
            "SELECT date,strategy,action,sampled FROM strategy_history ORDER BY id").fetchall()]

    # ---------- 立场账本 ----------
    def set_stance(self, date, target, direction, intent="", quote="", source="chat", days=None):
        direction = str(direction or "").strip()
        if direction not in STANCE_DEFAULT_DAYS:
            if re.search(r"观望|不动|等等|再看看|先不|谨慎", direction):
                direction = "观望"
            elif re.search(r"看好|看多|想买|加仓", direction):
                direction = "看多"
            elif re.search(r"看空|想卖|减仓|躲|规避", direction):
                direction = "看空"
            else:
                direction = "观望"
        d = int(days if days is not None else STANCE_DEFAULT_DAYS.get(direction, 3))
        try:
            ex = (_date.fromisoformat(date) + timedelta(days=d)).isoformat()
        except Exception:
            ex = date
        c = self.conn()
        c.execute("UPDATE stances SET status='superseded' WHERE status='active' AND target=?",
                  (str(target)[:40],))
        c.execute("INSERT INTO stances(date,expire_date,target,direction,intent,quote,status,source) "
                  "VALUES(?,?,?,?,?,?,'active',?)",
                  (date, ex, str(target)[:40], direction, str(intent)[:40],
                   str(quote)[:200], str(source)[:20]))
        c.commit()
        return {"ok": True, "direction": direction, "expire_date": ex}

    def active_stances(self, date, include_today=True):
        bound = "<=" if include_today else "<"
        rows = self.conn().execute(
            f"SELECT * FROM stances WHERE status='active' AND date{bound}? AND expire_date>=? ORDER BY id DESC",
            (date, date)).fetchall()
        return [dict(r) for r in rows]

    def mark_stance_fulfilled(self, sid):
        self.conn().execute("UPDATE stances SET status='fulfilled' WHERE id=?", (sid,))
        self.conn().commit()

    def stance_line(self, date, for_decide=False):
        rows = self.active_stances(date)
        if not rows:
            return ""
        parts = []
        for r in rows[:5]:
            seg = f"{r['target']}·{r['direction']}"
            if r["intent"]:
                seg += f"（{r['intent']}）"
            parts.append(seg)
        head = ("你自己最近对外表过态的方向（这是你自己说过的话，决策要与它对齐；要改口就在理由里说清为什么）"
                if for_decide else
                "你先前表过态的方向（被问起未来思路时可据此说，别推说没想过）")
        return "\n【" + ("立场·决策" if for_decide else "立场·自述") + "】" + head + "：" + "；".join(parts) + "。"

    def note_stance_consistency(self, date, proposal):
        rows = self.active_stances(date)
        if not rows:
            return False, ""
        action = proposal.get("action", "hold")
        code = proposal.get("code", "")
        rel = [r for r in rows if code and code in r["target"]] or rows[:1]
        if not rel:
            return False, ""
        st = rel[0]
        d = st["direction"]
        conflict = ((d in ("观望", "先不动") and action in ("buy", "sell")) or
                    (d in ("想卖", "看空") and action == "buy") or
                    (d in ("想买", "看多") and action == "sell"))
        if not conflict:
            return False, ""
        note = f"改口说明：先前说过对{st['target']}{st['direction']}，这次按行情变化调整"
        try:
            self.conn().execute("INSERT INTO advice(date,say,did,res,up) VALUES(?,?,?,?,1)",
                (date, f"立场:{st['target']}·{d}", f"实际:{action} {code}", note))
            self.conn().commit()
            self.mark_stance_fulfilled(st["id"])
        except Exception:
            pass
        return True, note

    def commit_to_wait(self, date, code, quote, through=None):
        """显式承诺在指定日期（含首尾）内不给该标的开新仓。"""
        through = through or date
        if code not in self._watch or not quote.strip() or _date.fromisoformat(through) < _date.fromisoformat(date):
            raise ValueError("承诺需要自选标的、原话和有效日期")
        with self._lock, self.conn() as conn:
            return conn.execute(
                "INSERT INTO entry_commitments(code,starts,through,quote) VALUES(?,?,?,?)",
                (code, date, through, quote)).lastrowid

    def revise_commitment(self, commitment_id, date, reason):
        """记录显式改口，原始承诺及理由都保留。"""
        _date.fromisoformat(date)
        if not reason.strip():
            raise ValueError("改口需要理由")
        with self._lock, self.conn() as conn:
            result = conn.execute(
                "UPDATE entry_commitments SET revised_on=?,revision=? "
                "WHERE id=? AND revised_on IS NULL AND starts<=? AND through>=?",
                (date, reason, commitment_id, date, date))
            if not result.rowcount:
                raise ValueError("指定日期没有这条可修订的承诺")

    def commitments_on(self, date):
        return [dict(row) for row in self.conn().execute(
            "SELECT id,code,starts,through,quote FROM entry_commitments "
            "WHERE starts<=? AND through>=? AND (revised_on IS NULL OR revised_on>?) ORDER BY id",
            (date, date, date))]

    def commitment(self, commitment_id):
        row = self.conn().execute("SELECT * FROM entry_commitments WHERE id=?",
                                  (commitment_id,)).fetchone()
        return dict(row) if row else None

    # ---------- 规则闸门 ----------
    def rule_snapshot(self, proposal, market, date, block_date=None, allowed_buys=None, fee_rate=0):
        """只收集订单校验所需事实，纯规则可从快照重跑。"""
        from .market import close_on, history_upto
        code = proposal.get("code", "")
        action = proposal.get("action", "hold")
        price = close_on(market, date, code) if action != "hold" else None
        history = history_upto(market, date, code, 2) if action == "buy" else []
        change = ((history[-1] / history[-2] - 1) * 100
                  if len(history) == 2 and history[-2] > 0 else None)
        position = next((p for p in self.positions() if p["code"] == code), None)
        return {
            "date": date, "blocked": block_date == date, "watched": code in self._watch,
            "price": price, "daily_change_pct": change, "equity": self.equity(market, date),
            "cash": self.cash(), "shares": position["shares"] if position else 0.0,
            "cost": position["cost"] if position else 0.0, "fee_rate": fee_rate,
            "managed_position": action == "buy" and bool(position) and self.conn().execute(
                "SELECT 1 FROM strategy_entries WHERE code=? AND close_id IS NULL", (code,)).fetchone() is not None,
            "rules": self.active_rules(), "allowed_buys": allowed_buys,
            "commitment": next((item for item in self.commitments_on(date)
                                if item["code"] == code), None),
        }

    def check_rules(self, proposal, market, date, block_date=None):
        from .execution import evaluate_order
        check = evaluate_order(proposal, self.rule_snapshot(proposal, market, date, block_date))
        why = check["gate"] if check["allowed"] else check["reason"]
        return check["allowed"], why, check["checks"]

    # ---------- 决策与执行回执 ----------
    @staticmethod
    def _decision_row(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("proposal", "policy", "submission", "outcome", "audit"):
            if result[key] is not None:
                result[key] = json.loads(result[key])
        return result

    def decision(self, decision_id):
        return self._decision_row(self.conn().execute(
            "SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone())

    def decision_on(self, date):
        return self._decision_row(self.conn().execute(
            "SELECT * FROM decisions WHERE date=?", (date,)).fetchone())

    def latest_decision(self):
        return self._decision_row(self.conn().execute(
            "SELECT * FROM decisions ORDER BY date DESC LIMIT 1").fetchone())

    def decision_history(self, limit=100):
        rows = self.conn().execute("SELECT * FROM decisions ORDER BY date DESC LIMIT ?", (limit,))
        return [self._decision_row(row) for row in rows]

    def stage_decision(self, proposal, policy, market, date, block_date, agent_input=None):
        from .execution import evaluate_order
        with self._lock, self.conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self.decision_on(date)
            if existing:
                return existing
            previous = self.latest_decision()
            if previous and (date < previous["date"] or previous["outcome"] is None):
                raise ValueError("请先按时间顺序完成上一条决策")
            facts = self.rule_snapshot(proposal, market, date, block_date, policy["allowed_buys"],
                                       policy["fee_bps"] / 10000)
            check = evaluate_order(proposal, facts)
            audit = (json.dumps({"version": 1, "agent_input": agent_input, "submission": facts},
                                ensure_ascii=False) if agent_input is not None else None)
            cursor = conn.execute(
                "INSERT INTO decisions(date,proposal,policy,submission,audit) VALUES(?,?,?,?,?)",
                (date, json.dumps(proposal, ensure_ascii=False), json.dumps(policy, ensure_ascii=False),
                 json.dumps(check, ensure_ascii=False), audit))
            if policy["allowed_buys"] is not None:
                arm = policy["arm"]
                conn.execute(
                    "INSERT INTO strategy_history(date,strategy,action,sampled) VALUES(?,?,?,?)",
                    (date, arm, proposal["action"], policy["draws"].get(arm, 0)))
            return self.decision(cursor.lastrowid)

    # ---------- 成交 + hash 链 ----------
    def _last_hash(self):
        r = self.conn().execute("SELECT hash FROM trades ORDER BY id DESC LIMIT 1").fetchone()
        return r["hash"] if r and r["hash"] else GENESIS_HASH

    @staticmethod
    def _hash_row(prev_hash, payload):
        # 数值统一转 float：写入时 0(int) 与从 SQLite 读回 0.0(float) 必须序列化一致，
        # 否则 json.dumps 得 "0" vs "0.0"，链校验假失败。
        norm = {}
        for k, v in payload.items():
            if isinstance(v, bool):
                norm[k] = v
            elif isinstance(v, (int, float)):
                norm[k] = float(v)
            else:
                norm[k] = str(v)
        raw = prev_hash + json.dumps(norm, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def execute(self, proposal, market, date, src="自主决策", block_date=None, *, decision_id=None):
        from .execution import evaluate_order
        from .market import market_dates
        with self._lock, self.conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            decision = self.decision(decision_id) if decision_id is not None else None
            if decision_id is not None and decision is None:
                raise ValueError("未知决策")
            if decision and decision["outcome"] is not None:
                return decision["outcome"]
            if decision:
                proposal = decision["proposal"]
            action = proposal.get("action", "hold")
            code = proposal.get("code", "")
            policy = decision["policy"] if decision else {}
            strategy = policy.get("arm", "")
            queued = decision and decision["submission"]["allowed"] and action in ("buy", "sell")
            deferred = queued and policy["execution"] == "next_close"
            if decision:
                signal_date = decision["date"]
                fill_date = next((day for day in market_dates(market) if day > signal_date), None) if deferred else signal_date
                if date != fill_date:
                    raise ValueError("成交时点与已保存决策不符，请按交易日顺序推进")
            facts = self.rule_snapshot(proposal, market, date, block_date, policy.get("allowed_buys"),
                                       policy.get("fee_bps", 0) / 10000)
            check = evaluate_order(proposal, facts)
            if decision and not decision["submission"]["allowed"]:
                check = decision["submission"]

            seq = int(self.meta_get("seq", "0"))
            price = shares = value = fee = 0.0
            result = "未结算"
            outcome = {"executed": check["allowed"], "date": date, "gate": check["gate"]}
            booked = action if check["allowed"] else "rejected"
            if booked == "rejected":
                result = "已拒绝"
                outcome.update(status="rejected", reject_reason=check["reason"])
                if "commitment_id" in check:
                    outcome["commitment_id"] = check["commitment_id"]
            elif booked == "hold":
                outcome.update(status="hold", action="hold")
            elif booked == "buy":
                price = facts["price"]
                value = float(proposal["weight"]) * facts["equity"] - facts["shares"] * price
                shares = value / price
                fee = value * facts["fee_rate"]
                conn.execute("UPDATE positions SET shares=shares-? WHERE code='CASH'", (value + fee,))
                total_shares = facts["shares"] + shares
                cost = (facts["shares"] * facts["cost"] + value + fee) / total_shares
                conn.execute(
                    "INSERT INTO positions(code,shares,cost) VALUES(?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET shares=excluded.shares,cost=excluded.cost",
                    (code, total_shares, cost))
                result = "持有中"
                outcome.update(status="filled", action=action, code=code, price=price, fee=fee)
            else:
                price, shares = facts["price"], facts["shares"]
                value = shares * price
                fee = value * facts["fee_rate"]
                pnl = ((value - fee) / (shares * facts["cost"]) - 1) * 100 if facts["cost"] else 0.0
                conn.execute("UPDATE positions SET shares=shares+? WHERE code='CASH'", (value - fee,))
                conn.execute("DELETE FROM positions WHERE code=?", (code,))
                result = f"{pnl:+.1f}%"
                outcome.update(status="filled", action=action, code=code, price=price, fee=fee, pnl_pct=pnl)

            reason = proposal.get("reason", "")
            prev = self._last_hash()
            checksum = self._hash_row(prev, {"date": date, "seq": seq, "code": code, "action": booked,
                                             "price": price, "value": value, "result": result, "reason": reason})
            trade_id = conn.execute(
                "INSERT INTO trades(date,seq,code,name,action,price,shares,value,reason,confidence,"
                "source,strategy,gates,result,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (date, seq, code, self._watch.get(code, code), booked, price, shares, value, reason,
                 proposal.get("confidence", 0), src, strategy, json.dumps(check["checks"], ensure_ascii=False),
                 result, prev, checksum)).lastrowid
            outcome["trade_id"] = trade_id
            if booked == "buy" and policy.get("allowed_buys") is not None:
                conn.execute(
                    "INSERT INTO strategy_entries(entry_id,decision_id,code,arm,brain) VALUES(?,?,?,?,?)",
                    (trade_id, decision_id, code, strategy, proposal["brain"]))
                outcome["entry_id"] = trade_id
            elif booked == "sell":
                origin = conn.execute(
                    "SELECT entry_id,decision_id,arm,brain FROM strategy_entries WHERE code=? AND close_id IS NULL",
                    (code,)).fetchone()
                if origin:
                    before = self.verified_strategy_stats()[origin["arm"]]
                    conn.execute("UPDATE strategy_entries SET close_id=?,pnl_pct=? WHERE entry_id=?",
                                 (trade_id, pnl, origin["entry_id"]))
                    after = self.verified_strategy_stats()[origin["arm"]]
                    outcome["feedback"] = {
                        **dict(origin), "close_id": trade_id, "win": pnl > 0, "before": before, "after": after,
                    }
            if booked in ("buy", "sell"):
                self.meta_set("seq", str(seq + 1), commit=False)
            self.record_nav(market, date, commit=False)
            if decision:
                audit = decision["audit"]
                if audit is not None and decision["submission"]["allowed"]:
                    audit["execution"] = facts
                    audit["execution_check"] = check
                conn.execute("UPDATE decisions SET outcome=?,audit=? WHERE id=?",
                             (json.dumps(outcome, ensure_ascii=False),
                              json.dumps(audit, ensure_ascii=False) if audit is not None else None, decision_id))
            return outcome

    def record_nav(self, market, date, commit=True):
        """记一个交易日的收盘净值。编排层每个交易日都要调，hold/rejected 也算一天——
        否则净值曲线会缺天。ON CONFLICT 保证同日覆盖为最终值。"""
        seq = int(self.meta_get("seq", "0"))
        self.conn().execute(
            "INSERT INTO nav(date,seq,total,cash) VALUES(?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET seq=excluded.seq,total=excluded.total,cash=excluded.cash",
            (date, seq, self.equity(market, date), self.cash()))
        if commit:
            self.conn().commit()

    # 旧名兼容（内部/旧测试可能引用）
    _record_nav = record_nav

    def verify_chain(self):
        """校验 hash 链完整性，返回 (ok, broken_id)。"""
        rows = self.conn().execute("SELECT id,prev_hash,hash,date,seq,code,action,price,value,result,reason FROM trades ORDER BY id").fetchall()
        prev = GENESIS_HASH
        for r in rows:
            expect = self._hash_row(prev, {"date": r["date"], "seq": r["seq"], "code": r["code"],
                "action": r["action"], "price": r["price"], "value": r["value"],
                "result": r["result"], "reason": r["reason"]})
            if r["prev_hash"] != prev or r["hash"] != expect:
                return False, r["id"]
            prev = r["hash"]
        return True, None

    def journal(self, limit=60):
        rows = self.conn().execute(
            "SELECT date,code,name,action,price,value,reason,result,gates,hash FROM trades "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["gates"] = json.loads(d["gates"]) if d["gates"] else []
            except Exception:
                d["gates"] = []
            out.append(d)
        return out

    def nav_series(self):
        return [dict(r) for r in self.conn().execute(
            "SELECT date,total,cash FROM nav ORDER BY date").fetchall()]

    def add_note(self, date, text):
        self.conn().execute("INSERT INTO notes(date,text) VALUES(?,?)", (date, text))
        self.conn().commit()

    def close(self):
        """关闭连接。Windows 下必须调用才能删库文件（测试清理用）。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
