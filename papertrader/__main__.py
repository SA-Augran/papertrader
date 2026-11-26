"""Run a user-owned account, with local assets and optional model or MCP tools."""
import argparse
from datetime import datetime
import logging
import os
from pathlib import Path
import threading
import webbrowser

from .service import AccountService, ServiceError, make_server, TZ


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "mcp"), nargs="?", default="serve")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".papertrader")
    parser.add_argument("--port", type=int, default=8322)
    parser.add_argument("--mode", choices=("external", "model", "local"), default="external")
    parser.add_argument("--csv", type=Path, help="Explicit user-supplied daily CSV")
    parser.add_argument("--sandbox", action="store_true", help="Allow historical/synthetic manual testing")
    parser.add_argument("--close-time", default="19:30", help="Asia/Shanghai daily decision window start")
    parser.add_argument("--persona", type=Path, help="Optional user-owned persona file")
    parser.add_argument("--open", action="store_true", help="Open the local panel with session login")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "mcp":
        from .mcp_server import run
        return run()
    if args.mode == "model":
        try:
            from dotenv import load_dotenv
            load_dotenv(args.data_dir / ".env")
        except ImportError:
            pass
        if not (os.getenv("ZHIPU_API_KEY") or os.getenv("DEEPSEEK_API_KEY")):
            parser.error("Model mode needs a provider key; use external or local mode otherwise")
    service = AccountService(args.data_dir, mode=args.mode, csv_path=args.csv,
                             sandbox=args.sandbox, close_time=args.close_time, character=args.persona)
    server = make_server(service, args.port)
    stopped = threading.Event()

    def worker():
        first = True
        while not stopped.is_set():
            window = not service.sandbox and datetime.now(TZ).strftime("%H:%M") >= service.close_time
            if first or window:
                service.refresh()
                first = False
            if stopped.is_set():
                break
            if window:
                try:
                    service.tick()
                except ServiceError:
                    pass  # Holidays, pause and stale quotes are visible in account state.
                except Exception:
                    logging.exception("Daily account task failed; next cycle will retry")
            stopped.wait(600)

    thread = threading.Thread(target=worker, name="papertrader-daily", daemon=True)
    thread.start()
    print(f"Panel: http://127.0.0.1:{server.server_port}/", flush=True)
    print(f"Account token: {service.token}", flush=True)
    print(f"Data: {service.directory}", flush=True)
    print("Sandbox: manual historical testing" if args.sandbox else "Daily account; keep this process running", flush=True)
    if args.open:
        webbrowser.open(f"http://127.0.0.1:{server.server_port}/#token={service.token}")
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        server.server_close()
        thread.join(timeout=30)
        service.close()


if __name__ == "__main__":
    main()
