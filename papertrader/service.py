"""Single-owner daily account service, shared by HTTP, the panel and MCP."""
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from math import isfinite
from pathlib import Path
import secrets
import subprocess
import sys
import threading
from urllib.parse import urlparse, parse_qs

from . import market
from .brain import Brain
from .sim import Simulator

TZ = timezone(timedelta(hours=8))
LOG = logging.getLogger(__name__)


class ServiceError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class AccountService:
    def __init__(self, directory, *, mode="external", csv_path=None, sandbox=False,
                 close_time="19:30", character=None, now=None):
        if mode not in ("external", "model", "local"):
            raise ValueError("mode must be external, model or local")
        datetime.strptime(close_time, "%H:%M")
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._owner = (self.directory / "service.lock").open("a+b")
        self._owner.seek(0)
        self._owner.write(b"0")
        self._owner.flush()
        self._owner.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._owner.close()
            raise ValueError("This account is already owned by another service")
        self.mode, self.sandbox, self.close_time = mode, sandbox, close_time
        self.csv_path = Path(csv_path).resolve() if csv_path else None
        self.now = now or (lambda: datetime.now(TZ))
        self.error = None
        self._refresh_lock = threading.Lock()
        self.sim = Simulator(self.directory / "account.db", market_data=[], offline=mode != "model",
                             audit=True, seed=31, execution="same_close", fee_bps=10)
        # A neutral agent framework. A user may explicitly load their own persona.
        self.sim.brain.persona = Path(character).read_text(encoding="utf-8") if character else ""
        self.sim.character = "custom" if character else "external"
        conn = self.sim.led.conn()
        conn.executescript("""
                CREATE TABLE IF NOT EXISTS service_tasks(
                    id TEXT PRIMARY KEY, date TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS service_events(
                    id INTEGER PRIMARY KEY, date TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS service_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        try:
            signature = json.dumps({"mode": mode, "sandbox": sandbox,
                                    "csv": str(self.csv_path) if self.csv_path else None}, sort_keys=True)
            row = conn.execute("SELECT value FROM service_settings WHERE key='configuration'").fetchone()
            if row and row[0] != signature:
                raise ValueError("Use a new account directory when changing mode or market source")
            with conn:
                conn.execute("INSERT OR IGNORE INTO service_settings VALUES('configuration',?)", (signature,))
                conn.execute("INSERT OR IGNORE INTO service_settings VALUES('token',?)", (secrets.token_urlsafe(32),))
        except Exception:
            self.close()
            raise
        self.token = self.sim.led.conn().execute(
            "SELECT value FROM service_settings WHERE key='token'").fetchone()[0]

    def close(self):
        self.sim.led.close()
        self._owner.close()

    def _emit(self, kind, data):
        with self.sim.led.conn() as conn:
            conn.execute("INSERT INTO service_events(date,kind,data) VALUES(?,?,?)",
                         (self.now().isoformat(), kind, json.dumps(data, ensure_ascii=False)))

    def refresh(self):
        with self._refresh_lock:
            try:
                if self.csv_path:
                    rows, source = market.read_snapshot(self.csv_path), "provided_csv"
                elif self.sandbox:
                    rows, source = market.synthetic(n=120, end="2025-12-31"), "synthetic"
                else:
                    fetched = subprocess.run([sys.executable, "-m", "papertrader.quote_worker"],
                                             capture_output=True, text=True, timeout=90, check=True)
                    rows, source = json.loads(fetched.stdout), "baostock"
                    if not rows:
                        raise ValueError("Empty market response")
                # Real mode never falls back to synthetic data.
                with self.sim.led._lock:
                    self.sim.market, self.sim.source = rows, source
                    self.error = None
                return source
            except Exception as exc:
                with self.sim.led._lock:
                    self.error = f"Market refresh failed ({type(exc).__name__})"
                LOG.exception("Market refresh failed")
                return None

    def _date(self):
        if self.error:
            raise ServiceError(self.error)
        dates = self.sim.dates()
        if not dates:
            raise ServiceError("No market data available")
        if self.sandbox:
            return dates[-1]
        now = self.now().astimezone(TZ)
        if now.weekday() >= 5 or now.strftime("%H:%M") < self.close_time:
            raise ServiceError("Outside the configured daily decision window")
        today = now.date().isoformat()
        if today not in dates:
            raise ServiceError("Today's completed quote is unavailable; waiting for refresh")
        return today

    def paused(self):
        return self.sim.led.meta_get("service_paused", "0") == "1"

    def set_paused(self, value):
        if not isinstance(value, bool):
            raise ServiceError("paused must be boolean", 400)
        with self.sim.led._lock:
            self.sim.led.meta_set("service_paused", "1" if value else "0")
            self._emit("pause", {"paused": value})
        return {"paused": value}

    def context(self):
        with self.sim.led._lock:
            if self.paused():
                raise ServiceError("Account is paused")
            date = self._date()
            conn = self.sim.led.conn()
            pending = conn.execute("SELECT id,date FROM decisions WHERE outcome IS NULL ORDER BY id LIMIT 1").fetchone()
            if pending and pending["date"] != date:
                raise ServiceError("An earlier staged decision must be recovered before a new task")
            row = conn.execute("SELECT payload FROM service_tasks WHERE date=?", (date,)).fetchone()
            if row:
                return json.loads(row[0])
            policy = self.sim.brain.strategy_policy(self.sim.market, date)
            policy.update(execution=self.sim.execution, fee_bps=self.sim.fee_bps)
            observation = self.sim.brain.decision_snapshot(self.sim.market, date)
            backgrounds = conn.execute(
                "SELECT data FROM service_events WHERE kind='observation' AND date<=? "
                "AND date>=? ORDER BY id DESC LIMIT 6",
                (date + "T23:59:59+08:00",
                 (datetime.fromisoformat(date) - timedelta(days=3)).date().isoformat())).fetchall()
            task = {"task_id": secrets.token_urlsafe(18), "date": date, "source": self.sim.source,
                    "sandbox": self.sandbox, "policy": policy, "observation": observation,
                    "allowed_actions": {"buy": policy["allowed_buys"],
                                        "sell": [p["code"] for p in observation["positions"]], "hold": True},
                    "lessons": self.sim.brain.lessons_line(),
                    "background": [json.loads(row[0]) for row in reversed(backgrounds)]}
            with conn:
                conn.execute("INSERT INTO service_tasks VALUES(?,?,?)",
                             (task["task_id"], date, json.dumps(task, ensure_ascii=False)))
            return task

    @staticmethod
    def _proposal(data):
        if not isinstance(data, dict):
            raise ServiceError("proposal must be an object", 400)
        action = data.get("action")
        if action not in ("buy", "sell", "hold"):
            raise ServiceError("action must be buy, sell or hold", 400)
        reason, code = data.get("reason"), data.get("code", "")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ServiceError("A reason of 1–1000 characters is required", 400)
        if not isinstance(code, str):
            raise ServiceError("code must be a string", 400)
        try:
            weight = float(data.get("weight", 0))
        except (TypeError, ValueError):
            raise ServiceError("weight must be finite", 400)
        if not isfinite(weight):
            raise ServiceError("weight must be finite", 400)
        # Keep the proposed weight intact so the shared gate can reject it.
        return {"action": action, "code": code, "weight": weight, "reason": reason.strip()}

    def submit(self, task_id, data, *, source="external"):
        if not isinstance(task_id, str):
            raise ServiceError("task_id must be a string", 400)
        if self.mode != "external" and source == "external":
            raise ServiceError("This account uses its built-in decision runner")
        proposal = self._proposal(data)
        with self.sim.led._lock:
            row = self.sim.led.conn().execute("SELECT payload FROM service_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ServiceError("Unknown task", 404)
            task = json.loads(row[0])
            existing = self.sim.led.decision_on(task["date"])
            if existing:
                keys = ("action", "code", "weight", "reason")
                if any(existing["proposal"].get(k) != proposal[k] for k in keys):
                    raise ServiceError("This task already has a different decision")
                # A staged order with no outcome is resumed after an interrupted execution.
                if existing["outcome"] is None:
                    self.sim._finish_day(existing, None, None, True)
                return self.sim.led.decision(existing["id"])
            if self.paused() or task["date"] != self._date():
                raise ServiceError("Task is paused or expired")
            earlier = self.sim.led.latest_decision()
            if earlier and earlier["date"] > task["date"]:
                raise ServiceError("A newer decision already exists")
            proposal.update(brain=source, strategy=task["policy"]["arm"])
            provider = self.sim.brain.primary if source == "primary" else self.sim.brain.secondary if source == "secondary" else None
            if provider:
                proposal["model"] = provider["model"]
            record = self.sim.led.stage_decision(proposal, task["policy"], self.sim.market, task["date"],
                                                 None, agent_input=task["observation"])
            self.sim._finish_day(record, None, None, True)
            # The decision table is the durable event source even if this process
            # exits before returning the response.
            return self.sim.led.decision(record["id"])

    def tick(self):
        task = self.context()
        if self.mode == "external":
            return {"waiting_for_agent": True, "task_id": task["task_id"]}
        with self.sim.led._lock:
            existing = self.sim.led.decision_on(task["date"])
            if existing:
                return self.submit(task["task_id"], existing["proposal"], source=existing["proposal"]["brain"])
            proposal = self.sim.brain.decide(self.sim.market, task["date"],
                                              ww_feed=task["background"],
                                              policy=task["policy"], snapshot=task["observation"])
            return self.submit(task["task_id"], proposal, source=proposal["brain"])

    def state(self):
        with self.sim.led._lock:
            overview = self.sim.overview()
            date = self.sim.dates()[-1] if self.sim.market else None
            positions = []
            for p in overview["positions"]:
                price = market.price_of(self.sim.market, date, p["code"]) if date else p["cost"]
                price = price or p["cost"]
                positions.append({**p, "name": market.NAME_BY_CODE.get(p["code"], p["code"]),
                                  "price": price, "value": p["shares"] * price,
                                  "pnl_pct": (price / p["cost"] - 1) * 100 if p["cost"] else 0})
            total = overview["cash"] + sum(p["value"] for p in positions)
            fills = [dict(row) for row in self.sim.led.conn().execute(
                "SELECT id,date,code,name,action,price,shares,value,result FROM trades "
                "ORDER BY id DESC LIMIT 100")]
            quotes = self.sim.brain.decision_snapshot(self.sim.market, date)["quotes"] if date else {}
            return {**overview, "equity": round(total, 2),
                    "return_pct": round((total / self.sim.led.invest_base() - 1) * 100, 2),
                    "positions": positions, "mode": self.mode, "sandbox": self.sandbox,
                    "market_date": date, "market_error": self.error, "paused": self.paused(),
                    "close_time": self.close_time, "timezone": "Asia/Shanghai",
                    "execution": self.sim.execution, "fee_bps_per_side": self.sim.fee_bps,
                    "strategy": self.sim.led.verified_strategy_stats(),
                    "decisions": self.sim.led.decision_history(100),
                    "diary": self.sim.diary(20), "rule_history": self.sim.led.rule_history(),
                    "quotes": quotes, "fills": fills, "invested": self.sim.led.invest_base()}

    def events(self, since=0):
        with self.sim.led._lock:
            rows = self.sim.led.conn().execute(
                "SELECT * FROM decisions WHERE id>? AND outcome IS NOT NULL ORDER BY id LIMIT 50", (since,)).fetchall()
            items = []
            changes = self.sim.led.rule_history()
            for row in rows:
                record = self.sim.led._decision_row(row)
                record["rule_changes"] = [change for change in changes
                                          if change["date"] == record["date"]]
                items.append(record)
            return {"items": items,
                    "cursor": rows[-1]["id"] if rows else since}

    def commitment(self, code, quote, through=None):
        with self.sim.led._lock:
            date = self._date()
            if not isinstance(quote, str) or not 0 < len(quote) <= 1000:
                raise ServiceError("Invalid commitment quote", 400)
            if through and through > (datetime.fromisoformat(date) + timedelta(days=30)).date().isoformat():
                raise ServiceError("Commitment may last at most 30 days", 400)
            identifier = self.sim.led.commit_to_wait(date, code, quote, through)
            return {"id": identifier}

    def observation(self, text, source):
        if not isinstance(text, str) or not 0 < len(text) <= 2000:
            raise ServiceError("Observation text must be 1–2000 characters", 400)
        if not isinstance(source, str) or not 0 < len(source) <= 200:
            raise ServiceError("Observation source must be 1–200 characters", 400)
        with self.sim.led._lock:
            self._emit("observation", {"text": text, "source": source, "received_at": self.now().isoformat()})
        return {"ok": True}


def make_server(service, port=8322):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format, *args):
            LOG.info("%s %s", self.command, urlparse(self.path).path)

        def _reply(self, status, data, content_type="application/json; charset=utf-8"):
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode() if isinstance(data, (dict, list)) else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _handle(self):
            path = urlparse(self.path).path
            if self.headers.get("Host") not in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"):
                return self._reply(403, {"error": "Invalid host"})
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.css": ("app.css", "text/css; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/assets/bg-snow-deep.jpg": ("assets/bg-snow-deep.jpg", "image/jpeg"),
                      "/vendor/echarts.min.js": ("vendor/echarts.min.js", "text/javascript; charset=utf-8")}
            if self.command == "GET" and path in assets:
                name, mime = assets[path]
                return self._reply(200, files("papertrader").joinpath("web", name).read_bytes(), mime)
            provided = self.headers.get("Authorization", "")
            if not hmac.compare_digest(provided, "Bearer " + service.token):
                return self._reply(401, {"error": "Account token required"})
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"):
                return self._reply(403, {"error": "Cross-origin access refused"})
            if self.command == "GET":
                query = parse_qs(urlparse(self.path).query)
                if path == "/api/state":
                    return self._reply(200, service.state())
                if path == "/api/context":
                    return self._reply(200, service.context())
                if path == "/api/events":
                    cursor = int(query.get("since", ["0"])[0])
                    if cursor < 0:
                        raise ServiceError("Invalid cursor", 400)
                    return self._reply(200, service.events(cursor))
            if self.command == "POST":
                if self.headers.get_content_type() != "application/json":
                    raise ServiceError("application/json required", 415)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ServiceError("Request body must be 1–65536 bytes", 413)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ServiceError("JSON object required", 400)
                if path == "/api/submit":
                    return self._reply(200, service.submit(data.get("task_id"), data.get("proposal")))
                if path == "/api/tick":
                    return self._reply(200, service.tick())
                if path == "/api/pause":
                    return self._reply(200, service.set_paused(data.get("paused")))
                if path == "/api/commitment":
                    return self._reply(200, service.commitment(data.get("code"), data.get("quote"), data.get("through")))
                if path == "/api/observation":
                    return self._reply(200, service.observation(data.get("text"), data.get("source")))
                if path == "/api/revise":
                    with service.sim.led._lock:
                        service.sim.led.revise_commitment(data["id"], service._date(), data["reason"])
                    return self._reply(200, {"ok": True})
                if path == "/api/recheck":
                    with service.sim.led._lock:
                        result = service.sim.recheck(data["decision_id"], stage=data.get("stage", "submission"),
                                                    ignore_commitment=data.get("ignore_commitment") is True)
                    return self._reply(200, result)
            return self._reply(404, {"error": "Unknown endpoint"})

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def dispatch(self):
            try:
                self._handle()
            except ServiceError as exc:
                self._reply(exc.status, {"error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                self._reply(400, {"error": str(exc)})
            except Exception:
                LOG.exception("Account request failed")
                self._reply(500, {"error": "Account operation failed; retry the same task"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
