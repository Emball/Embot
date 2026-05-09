import asyncio
import json
import secrets
import socket
import sqlite3
import re
import sys
import os
import argparse
import urllib.request
import urllib.parse
import urllib.error
import ssl
from pathlib import Path
from datetime import datetime
from aiohttp import web

from _utils import script_dir, migrate_config, atomic_json_write

MODULE_NAME = "REMOTE_DEBUG"

RD_CONFIG_PATH = script_dir() / "config" / "remote_debug.json"
RD_CONFIG_DEFAULTS = {
    "enabled": False,
    "host": "0.0.0.0",
    "port": 8765,
    "token": "",
    "allowed_ips": [],
    "url": "http://192.168.1.100:8765",
}


def _migrate_client_config():
    client_cfg = script_dir() / "temp" / "remote.json"
    if not client_cfg.exists():
        return
    try:
        with open(client_cfg, "r") as f:
            client_data = json.load(f)
        cfg = _load_config()
        if client_data.get("url"):
            cfg["url"] = client_data["url"]
        if client_data.get("token") and not cfg.get("token"):
            cfg["token"] = client_data["token"]
        atomic_json_write(RD_CONFIG_PATH, cfg)
        client_cfg.unlink()
    except Exception as e:
        print(f"[{MODULE_NAME}] Failed to migrate temp/remote.json: {e}", file=sys.stderr)


def _load_config() -> dict:
    cfg = migrate_config(RD_CONFIG_PATH, RD_CONFIG_DEFAULTS)
    if not cfg.get("token", "").strip():
        cfg["token"] = secrets.token_hex(32)
        atomic_json_write(RD_CONFIG_PATH, cfg)
    return cfg


def _detect_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            s.connect(("1.1.1.1", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"


class RemoteDebugServer:
    def __init__(self, bot):
        self.bot = bot
        self._config = _load_config()
        self._app = web.Application()
        self._runner = None
        self._setup_routes()

    def _setup_routes(self):
        self._app.router.add_get("/ping", self._handle_ping)
        self._app.router.add_get("/status", self._handle_status)
        self._app.router.add_get("/logs", self._handle_logs)
        self._app.router.add_get("/logs/stream", self._handle_logs_stream)
        self._app.router.add_get("/db/{name}", self._handle_db_download)
        self._app.router.add_get("/db/{name}/query", self._handle_db_query)
        self._app.router.add_get("/config/{name}", self._handle_config)
        self._app.router.add_post("/update", self._handle_update)
        self._app.router.add_post("/restart", self._handle_restart)
        self._app.middlewares.append(self._error_middleware)

    @web.middleware
    async def _error_middleware(self, request, handler):
        try:
            return await handler(request)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"API error on {request.path}", e)
            return web.json_response(
                {"error": str(e), "type": type(e).__name__},
                status=500,
            )

    def _check_auth(self, request) -> bool:
        req_token = request.headers.get("X-Debug-Token", "")
        return req_token == self._config.get("token", "")

    def _check_ip(self, request) -> bool:
        allowed = self._config.get("allowed_ips", [])
        if not allowed:
            return True
        peer = request.remote
        return peer in allowed

    def _fail(self, msg="unauthorized", status=403) -> web.Response:
        return web.json_response({"error": msg}, status=status)

    def _sanitize_name(self, name: str) -> bool:
        return bool(re.match(r'^[a-zA-Z0-9_\-]+$', name))

    async def _handle_ping(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        return web.json_response({"ok": True, "time": datetime.now().isoformat()})

    async def _handle_status(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        return web.json_response({
            "version": getattr(self.bot, "version", "unknown"),
            "latency": round(getattr(self.bot, "latency", 0) * 1000, 1),
            "guilds": len(self.bot.guilds),
            "user": str(self.bot.user) if self.bot.user else None,
            "log_file": str(self.bot.logger.log_file.name) if getattr(self.bot.logger, "log_file", None) else None,
        })

    async def _handle_logs(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        if not hasattr(self.bot.logger, "log_file") or not self.bot.logger.log_file.exists():
            return web.json_response({"error": "no log file"}, status=404)
        try:
            lines = min(int(request.query.get("lines", "200")), 20000)
        except ValueError:
            lines = 200
        log_path = self.bot.logger.log_file
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        return web.Response(
            text="".join(all_lines[-lines:]),
            content_type="text/plain",
            charset="utf-8",
        )

    async def _handle_logs_stream(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        if not hasattr(self.bot.logger, "log_file") or not self.bot.logger.log_file.exists():
            return web.json_response({"error": "no log file"}, status=404)
        log_path = self.bot.logger.log_file

        response = web.StreamResponse()
        response.content_type = "text/event-stream"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Connection"] = "keep-alive"
        response.headers["X-Accel-Buffering"] = "no"
        await response.prepare(request)

        last_size = log_path.stat().st_size if log_path.exists() else 0
        try:
            while not request.transport.is_closing():
                if log_path.exists():
                    current_size = log_path.stat().st_size
                    if current_size > last_size:
                        with open(log_path, "r", encoding="utf-8") as f:
                            f.seek(last_size)
                            new_data = f.read()
                        for line in new_data.split("\n"):
                            if line.strip():
                                try:
                                    await response.write(f"data: {line}\n\n".encode("utf-8"))
                                except ConnectionResetError:
                                    return response
                        last_size = current_size
                await asyncio.sleep(0.5)
        except (ConnectionResetError, ConnectionAbortedError):
            pass
        return response

    async def _handle_db_download(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        name = request.match_info["name"]
        if not self._sanitize_name(name):
            return self._fail("invalid db name", 400)
        db_path = script_dir() / "db" / f"{name}.db"
        if not db_path.exists():
            return web.json_response({"error": "db not found"}, status=404)
        return web.FileResponse(db_path)

    async def _handle_db_query(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        name = request.match_info["name"]
        if not self._sanitize_name(name):
            return self._fail("invalid db name", 400)
        db_path = script_dir() / "db" / f"{name}.db"
        if not db_path.exists():
            return web.json_response({"error": "db not found"}, status=404)
        query = request.query.get("q", "")
        if not query:
            return self._fail("missing query parameter 'q'", 400)
        q_upper = query.strip().upper()
        if not (q_upper.startswith("SELECT") or q_upper.startswith("PRAGMA") or q_upper.startswith("EXPLAIN")):
            return self._fail("only SELECT / PRAGMA / EXPLAIN queries allowed", 403)
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query)
            rows = [dict(row) for row in cur.fetchall()]
            conn.close()
            return web.json_response({"rows": rows, "count": len(rows)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_config(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        name = request.match_info["name"]
        if name.lower() in ("auth", "token"):
            return self._fail("access denied", 403)
        if not self._sanitize_name(name):
            return self._fail("invalid config name", 400)
        config_path = script_dir() / "config" / f"{name}.json"
        if not config_path.exists():
            return web.json_response({"error": "config not found"}, status=404)
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return web.json_response(data)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_update(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-c", "credential.helper=", "pull", "--ff-only", "origin", "main",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
            )
            stdout, stderr = await proc.communicate()
            out = stdout.decode().strip()
            err = stderr.decode().strip()
            if proc.returncode == 0:
                if "Already up to date" in out or "already up to date" in out:
                    return web.json_response({"updated": False, "output": out})
                self.bot.logger.log(MODULE_NAME, "Update pulled — restarting")
                asyncio.create_task(self._delayed_restart())
                return web.json_response({"updated": True, "output": out, "restarting": True})
            return web.json_response({"error": err or "git pull failed"}, status=500)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_restart(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        self.bot.logger.log(MODULE_NAME, "Remote restart requested")
        asyncio.create_task(self._delayed_restart())
        return web.json_response({"ok": True, "message": "Restarting..."})

    async def _delayed_restart(self):
        await asyncio.sleep(1)
        await self.bot.close()
        import os as _os
        import sys as _sys
        _os.execv(_sys.executable, [_sys.executable] + _sys.argv)

    async def start(self):
        if not self._config.get("enabled", True):
            self.bot.logger.log(MODULE_NAME, "Disabled by config")
            return
        host = self._config.get("host", "0.0.0.0")
        port = self._config.get("port", 8765)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()

        lan_ip = _detect_lan_ip()
        self.bot.logger.log(MODULE_NAME,
            f"Remote debug API online at http://{lan_ip}:{port}")
        self.bot.logger.log(MODULE_NAME,
            f"Auth token: {self._config['token']}")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self.bot.logger.log(MODULE_NAME, "HTTP debug server stopped")


def setup(bot):
    _migrate_client_config()
    bot.logger.log(MODULE_NAME, "Setting up remote debug HTTP server")
    config = _load_config()
    if not config.get("enabled", True):
        bot.logger.log(MODULE_NAME, "Remote debug disabled in config")
        return
    server = RemoteDebugServer(bot)
    bot.remote_debug_server = server

    async def _start():
        await bot.wait_until_ready()
        await server.start()

    bot._remote_debug_task = asyncio.create_task(_start())
    bot.logger.log(MODULE_NAME, "Remote debug module setup complete")


# ── Client mode (when run as `python modules/remote_debug.py <command>`) ──


def _load_client_config() -> dict:
    cfg = migrate_config(RD_CONFIG_PATH, RD_CONFIG_DEFAULTS)
    cfg["url"] = os.environ.get("REMOTE_URL", cfg["url"])
    cfg["token"] = os.environ.get("REMOTE_TOKEN", cfg["token"])
    return cfg


def _client_request(cfg, path, raw=False, method="GET"):
    url = cfg["url"].rstrip("/") + path
    data = b"" if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method)
    if cfg["token"]:
        req.add_header("X-Debug-Token", cfg["token"])
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            data = resp.read()
            if raw:
                return data
            return json.loads(data)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            err = json.loads(body).get("error", body)
        except Exception:
            err = body
        print(f"HTTP {e.code}: {err}")
        sys.exit(1)
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)


def _cmd_ping(cfg):
    print(json.dumps(_client_request(cfg, "/ping"), indent=2))


def _cmd_status(cfg):
    print(json.dumps(_client_request(cfg, "/status"), indent=2))


def _cmd_logs(cfg, lines=200):
    data = _client_request(cfg, f"/logs?lines={lines}", raw=True)
    sys.stdout.buffer.write(data + b"\n")


def _cmd_stream(cfg):
    url = cfg["url"].rstrip("/") + "/logs/stream"
    req = urllib.request.Request(url)
    if cfg["token"]:
        req.add_header("X-Debug-Token", cfg["token"])
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=3600, context=ctx) as resp:
            for line in resp:
                line = line.decode(errors="replace").strip()
                if line.startswith("data: "):
                    sys.stdout.buffer.write((line[6:] + "\n").encode("utf-8", errors="replace"))
    except KeyboardInterrupt:
        print("\nDisconnected.")
    except Exception as e:
        print(f"Stream error: {e}")
        sys.exit(1)


def _cmd_db_download(cfg, name):
    data = _client_request(cfg, f"/db/{name}", raw=True)
    out_path = script_dir() / "temp" / f"{name}.db"
    with open(out_path, "wb") as f:
        f.write(data)
    print(f"Saved {len(data):,} bytes to {out_path}")


def _cmd_db_query(cfg, name, query):
    encoded = urllib.parse.quote(query)
    result = _client_request(cfg, f"/db/{name}/query?q={encoded}")
    print(json.dumps(result, indent=2))


def _cmd_config(cfg, name):
    print(json.dumps(_client_request(cfg, f"/config/{name}"), indent=2))


def _cmd_update(cfg):
    print(json.dumps(_client_request(cfg, "/update", method="POST"), indent=2))


def _cmd_restart(cfg):
    print(json.dumps(_client_request(cfg, "/restart", method="POST"), indent=2))


def main():
    cfg = _load_client_config()

    parser = argparse.ArgumentParser(description="Embot remote debug client")
    parser.add_argument("--url", default=cfg["url"], help="Server URL")
    parser.add_argument("--token", default=cfg["token"], help="Auth token")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="Test connection")
    sub.add_parser("status", help="Bot status")

    logs_parser = sub.add_parser("logs", help="Fetch recent logs")
    logs_parser.add_argument("--lines", type=int, default=200)

    sub.add_parser("stream", help="Live log stream (Ctrl+C to stop)")

    db_dl = sub.add_parser("db-download", help="Download a database file")
    db_dl.add_argument("name", help="DB name (without .db)")

    db_q = sub.add_parser("db-query", help="Run a read-only SQL query")
    db_q.add_argument("name", help="DB name (without .db)")
    db_q.add_argument("query", help="SQL query (SELECT only)")

    config_p = sub.add_parser("config", help="View a config file")
    config_p.add_argument("name", help="Config name (without .json)")

    sub.add_parser("update", help="Git pull and restart if updated")
    sub.add_parser("restart", help="Restart the bot")

    args = parser.parse_args()
    run_cfg = {"url": args.url, "token": args.token}

    if args.command == "ping":
        _cmd_ping(run_cfg)
    elif args.command == "status":
        _cmd_status(run_cfg)
    elif args.command == "logs":
        _cmd_logs(run_cfg, args.lines)
    elif args.command == "stream":
        _cmd_stream(run_cfg)
    elif args.command == "db-download":
        _cmd_db_download(run_cfg, args.name)
    elif args.command == "db-query":
        _cmd_db_query(run_cfg, args.name, args.query)
    elif args.command == "config":
        _cmd_config(run_cfg, args.name)
    elif args.command == "update":
        _cmd_update(run_cfg)
    elif args.command == "restart":
        _cmd_restart(run_cfg)


if __name__ == "__main__":
    main()
