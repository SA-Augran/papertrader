"""Bounded-lifetime subprocess for third-party market data downloads."""
from contextlib import redirect_stdout
import json
import sys
from . import market


if __name__ == "__main__":
    with redirect_stdout(sys.stderr):
        rows = market.baostock()
    print(json.dumps(rows, ensure_ascii=True, allow_nan=False))
