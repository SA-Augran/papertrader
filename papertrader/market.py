# -*- coding: utf-8 -*-
"""market.py — 行情适配：真实日线（baostock）→ CSV 缓存 → 确定性合成兜底。

零网络也能跑：没装 baostock / 断网时用固定种子的合成行情，每次生成一致，便于复现和单测。
market 是「行字典」列表：[{date, code, close, ...}, ...]，按 code+date 索引。
"""
import csv
import random
from datetime import date as _date, timedelta
from math import isfinite
from pathlib import Path

WATCHLIST_CODES = [
    "600519", "300750", "601318", "000001",
    "002594", "600036", "000858", "601899",
]
NAME_BY_CODE = {
    "600519": "贵州茅台", "300750": "宁德时代", "601318": "中国平安",
    "000001": "平安银行", "002594": "比亚迪", "600036": "招商银行",
    "000858": "五粮液", "601899": "紫金矿业",
}
LOOKBACK_DAYS = 250


def market_dates(market):
    return sorted({r["date"] for r in market})


def price_of(market, date, code):
    """取 <= date 的最后一根收盘价（前视无污染）。"""
    px = None
    for r in market:
        if r["code"] == code and r["date"] <= date:
            px = r["close"]
        elif r["date"] > date and r["code"] == code and px is not None:
            break
    return px


def history_upto(market, date, code, n=30):
    h = [r["close"] for r in market if r["code"] == code and r["date"] <= date]
    return h[-n:]


def close_on(market, date, code):
    """成交需要当日行情。估值仍可用上一根收盘价。"""
    return next((r["close"] for r in market if r["code"] == code and r["date"] == date), None)


def ma(h, n):
    return sum(h[-n:]) / n if len(h) >= n else None


def _trading_days(n, end=None):
    """生成最近 n 个工作日（跳过周末），YYYY-MM-DD。end 可为 date 或 'YYYY-MM-DD' 字符串。"""
    end = end or _date.today()
    if isinstance(end, str):
        end = _date.fromisoformat(end)
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d -= timedelta(days=1)
    return list(reversed(days))


def synthetic(n=LOOKBACK_DAYS, end=None, seed=20261117):
    """确定性合成行情：固定种子随机游走，同一参数永远同一结果。"""
    days = _trading_days(n, end)
    rng = random.Random(seed)
    out = []
    base = {"600519": 1680.0, "300750": 205.0, "601318": 46.0, "000001": 11.2,
            "002594": 245.0, "600036": 34.0, "000858": 148.0, "601899": 16.5}
    for code in WATCHLIST_CODES:
        px = base.get(code, 20.0)
        for dt in days:
            drift = rng.gauss(0.0004, 0.018)
            px = max(0.5, px * (1 + drift))
            out.append({"date": dt, "code": code, "name": NAME_BY_CODE.get(code, code),
                        "close": round(px, 2)})
    return out


def baostock(n=LOOKBACK_DAYS):
    """真实 A 股日线（前复权）。需 pip install baostock + 联网。失败抛异常由上层兜底。"""
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")
    codes = ["sh." + c if c.startswith("6") else "sz." + c for c in WATCHLIST_CODES]
    end = _date.today()
    start = end - timedelta(days=int(n * 1.8))
    out = []
    try:
        for raw, code in zip(codes, WATCHLIST_CODES):
            rs = bs.query_history_k_data_plus(
                raw, "date,close", start_date=start.isoformat(), end_date=end.isoformat(),
                frequency="d", adjustflag="2")
            if rs.error_code != "0":
                continue
            while rs.next():
                row = rs.get_row_data()
                if row[1] in ("", "0"):
                    continue
                out.append({"date": row[0], "code": code,
                            "name": NAME_BY_CODE.get(code, code), "close": float(row[1])})
    finally:
        bs.logout()
    if not out:
        raise RuntimeError("baostock returned no data")
    return out


def load(cache_path=None, prefer_real=True, n=LOOKBACK_DAYS):
    """按优先级取行情：真实 → CSV 缓存 → 合成。source 标记实际来源。"""
    if prefer_real:
        try:
            data = baostock(n)
            if cache_path:
                _write_csv(cache_path, data)
            return data, "baostock"
        except Exception:
            pass
    if cache_path and Path(cache_path).exists():
        try:
            data = _read_csv(cache_path)
            if data:
                return data, "cache"
        except Exception:
            pass
    return synthetic(n), "synthetic"


def _write_csv(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "code", "name", "close"])
        w.writeheader()
        w.writerows(data)


def _read_csv(path):
    with Path(path).open(encoding="utf-8-sig") as f:
        return [{"date": r["date"], "code": r["code"], "name": r.get("name", ""),
                 "close": float(r["close"])} for r in csv.DictReader(f)]


def read_snapshot(path):
    """评测使用严格 CSV 输入。读取失败直接报错，便于辨认数据来源。"""
    rows = _read_csv(path)
    seen = set()
    for row in rows:
        date, code, price = row["date"], row["code"], row["close"]
        key = (code, date)
        if (_date.fromisoformat(date).isoformat() != date or len(code) != 6 or
                not code.isascii() or not code.isdigit() or not isfinite(price) or price <= 0 or key in seen):
            raise ValueError(f"无效或重复日线：{code} {date}")
        seen.add(key)
    if not rows:
        raise ValueError("行情 CSV 为空")
    return sorted(rows, key=lambda row: (row["code"], row["date"]))
