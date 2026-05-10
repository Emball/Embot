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
from datetime import datetime
import time
from aiohttp import web

from _utils import script_dir, migrate_config, atomic_json_write

MODULE_NAME = "REMOTE_DEBUG"

RD_CONFIG_PATH = script_dir() / "config" / "remote_debug.json"
RD_CONFIG_DEFAULTS = {
    "server": False,
    "host": "0.0.0.0",
    "port": 8765,
    "token": "",
    "allowed_ips": [],
    "claude_bridge": {
        "enabled": False,
        "repo": "Emball/EmbotDebug",
        "token": "",
        "poll_interval": 2.0,
    },
}


def _migrate_client_config():
    client_cfg = script_dir() / "temp" / "remote.json"
    if not client_cfg.exists():
        return
    try:
        with open(client_cfg, "r") as f:
            client_data = json.load(f)
        cfg = _load_config()
        if client_data.get("token") and not cfg.get("token"):
            cfg["token"] = client_data["token"]
        atomic_json_write(RD_CONFIG_PATH, cfg)
        client_cfg.unlink()
    except Exception as e:
        print(f"[{MODULE_NAME}] Failed to migrate temp/remote.json: {e}", file=sys.stderr)


def _load_config() -> dict:
    # preserve old enabled key before migrate_config drops it
    old = {}
    if RD_CONFIG_PATH.exists():
        try:
            with open(RD_CONFIG_PATH) as f:
                old = json.load(f)
        except Exception:
            pass
    old_enabled = old.get("enabled")

    cfg = migrate_config(RD_CONFIG_PATH, RD_CONFIG_DEFAULTS)

    # migrate old enabled → server
    if old_enabled is not None:
        cfg["server"] = bool(old_enabled)
        atomic_json_write(RD_CONFIG_PATH, cfg)

    for stale in ("url", "enabled"):
        if stale in cfg:
            del cfg[stale]
            atomic_json_write(RD_CONFIG_PATH, cfg)
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
        self._start_time: float = 0.0
        self._bridge = None
        self._setup_routes()

    def _setup_routes(self):
        self._app.router.add_get("/ping", self._handle_ping)
        self._app.router.add_get("/status", self._handle_status)
        self._app.router.add_get("/guilds", self._handle_guilds)
        self._app.router.add_get("/modules", self._handle_modules)
        self._app.router.add_get("/logs", self._handle_logs)
        self._app.router.add_get("/logs/list", self._handle_logs_list)
        self._app.router.add_get("/logs/search", self._handle_logs_search)
        self._app.router.add_get("/logs/{name}", self._handle_logs_file)
        self._app.router.add_get("/logs/stream", self._handle_logs_stream)
        self._app.router.add_get("/db/{name}", self._handle_db_download)
        self._app.router.add_get("/db/{name}/query", self._handle_db_query)
        self._app.router.add_get("/config/{name}", self._handle_config)
        self._app.router.add_post("/update", self._handle_update)
        self._app.router.add_post("/restart", self._handle_restart)
        self._app.router.add_post("/exec", self._handle_exec)
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
        uptime = int(time.time() - self._start_time) if self._start_time else 0
        guilds = list(self.bot.guilds)
        return web.json_response({
            "version": getattr(self.bot, "version", "unknown"),
            "latency": round(getattr(self.bot, "latency", 0) * 1000, 1),
            "guilds": len(guilds),
            "guilds_detail": [
                {"id": g.id, "name": g.name, "members": g.member_count}
                for g in guilds[:20]
            ],
            "uptime_seconds": uptime,
            "user": str(self.bot.user) if self.bot.user else None,
            "log_file": str(self.bot.logger.log_file.name) if getattr(self.bot.logger, "log_file", None) else None,
        })

    async def _handle_guilds(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        return web.json_response([
            {"id": g.id, "name": g.name, "members": g.member_count,
             "owner_id": g.owner_id, "channels": len(g.channels),
             "roles": len(g.roles), "created": str(g.created_at)}
            for g in self.bot.guilds
        ])

    async def _handle_modules(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        if not hasattr(self.bot.logger, "log_file") or not self.bot.logger.log_file.exists():
            return web.json_response({"error": "no log file"}, status=404)
        try:
            modules = []
            with open(self.bot.logger.log_file, "r", encoding="utf-8") as f:
                for line in f:
                    m = re.search(r"Loaded module: (\S+)", line)
                    if m:
                        modules.append(m.group(1))
            return web.json_response(modules)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    def _logs_dir(self):
        return script_dir() / "logs"

    async def _handle_logs_list(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        logs_dir = self._logs_dir()
        if not logs_dir.exists():
            return web.json_response([], status=200)
        files = []
        for fp in sorted(logs_dir.glob("session_*.log"), key=lambda x: x.stat().st_mtime, reverse=True):
            session_count = 0
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("--- Session"):
                            session_count += 1
            except Exception:
                pass
            files.append({
                "name": fp.name,
                "size": fp.stat().st_size,
                "mtime": datetime.fromtimestamp(fp.stat().st_mtime).isoformat(),
                "sessions": session_count,
            })
        return web.json_response(files)

    async def _handle_logs_search(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        query = request.query.get("q", "")
        if not query:
            return self._fail("missing query parameter 'q'", 400)
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            return self._fail(f"invalid regex: {e}", 400)
        try:
            max_results = min(int(request.query.get("max", "100")), 500)
        except ValueError:
            max_results = 100
        try:
            max_files = int(request.query.get("files", "0") or "0")
        except ValueError:
            max_files = 0

        logs_dir = self._logs_dir()
        if not logs_dir.exists():
            return web.json_response({"matches": [], "files_searched": 0})

        log_files = sorted(logs_dir.glob("session_*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
        if max_files > 0:
            log_files = log_files[:max_files]

        matches = []
        files_searched = 0
        for fp in log_files:
            files_searched += 1
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if pattern.search(line):
                            matches.append({
                                "file": fp.name,
                                "line": i,
                                "content": line.rstrip("\n"),
                            })
                            if len(matches) >= max_results:
                                break
                if len(matches) >= max_results:
                    break
            except Exception:
                continue

        return web.json_response({
            "query": query,
            "matches": matches,
            "total_matches": len(matches),
            "files_searched": files_searched,
            "truncated": len(matches) >= max_results,
        })

    async def _handle_logs_file(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        name = request.match_info["name"]
        if not re.match(r'^session_\d{8}(?:_\d{6})?\.log$', name):
            return self._fail("invalid log name", 400)
        log_path = self._logs_dir() / name
        if not log_path.exists():
            return web.json_response({"error": "log file not found"}, status=404)
        try:
            lines = min(int(request.query.get("lines", "200")), 20000)
        except ValueError:
            lines = 200
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        return web.Response(
            text="".join(all_lines[-lines:]),
            content_type="text/plain",
        )

    async def _handle_logs(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        log_name = request.query.get("file", "")
        if log_name:
            if not re.match(r'^session_\d{8}(?:_\d{6})?\.log$', log_name):
                return self._fail("invalid log name", 400)
            log_path = self._logs_dir() / log_name
        else:
            if not hasattr(self.bot.logger, "log_file") or not self.bot.logger.log_file.exists():
                return web.json_response({"error": "no log file"}, status=404)
            log_path = self.bot.logger.log_file
        if not log_path.exists():
            return web.json_response({"error": "log file not found"}, status=404)
        try:
            lines = min(int(request.query.get("lines", "200")), 20000)
        except ValueError:
            lines = 200
        session_num = request.query.get("session", "")
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        if session_num and session_num != "all":
            try:
                session_num = int(session_num)
            except ValueError:
                return self._fail("session must be a number or 'all'", 400)
            start = None
            end = None
            header_re = re.compile(r'^--- Session (\d+) ')
            for i, line in enumerate(all_lines):
                m = header_re.match(line)
                if m:
                    if int(m.group(1)) == session_num:
                        start = i
                    elif start is not None:
                        end = i
                        break
            if start is None:
                return web.json_response(
                    {"error": f"session {session_num} not found"}, status=404)
            out = all_lines[start:end]
            if lines and lines < len(out):
                out = out[-lines:]
        elif session_num == "all":
            out = all_lines
            if lines and lines < len(out):
                out = out[-lines:]
        else:
            out = all_lines[-lines:]
        return web.Response(
            text="".join(out),
            content_type="text/plain",
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
            while request.transport is not None and not request.transport.is_closing():
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

    async def _handle_exec(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        try:
            body = await request.json()
        except Exception:
            return self._fail("invalid JSON", 400)
        cmd = body.get("cmd", "").strip()
        if not cmd:
            return self._fail("missing 'cmd'", 400)
        timeout = min(int(body.get("timeout", 15)), 60)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return web.json_response({
                "exit_code": proc.returncode,
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
            })
        except asyncio.TimeoutError:
            return web.json_response({"error": "timed out"}, status=504)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _delayed_restart(self):
        await asyncio.sleep(1)
        await self.bot.close()
        import os as _os
        import sys as _sys
        _os.execv(_sys.executable, [_sys.executable] + _sys.argv)

    async def start(self):
        self._start_time = time.time()
        if self._config.get("server", False):
            host = self._config.get("host", "0.0.0.0")
            port = self._config.get("port", 8765)
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, host, port)
            await site.start()
            lan_ip = _detect_lan_ip()
            self.bot.logger.log(MODULE_NAME, f"Remote debug API online at http://{lan_ip}:{port}")
            self.bot.logger.log(MODULE_NAME, f"Auth token: {self._config['token']}")
        else:
            self.bot.logger.log(MODULE_NAME, "Server mode disabled in config")

        bridge_cfg = self._config.get("claude_bridge", {})
        if bridge_cfg.get("enabled") and bridge_cfg.get("token"):
            self._bridge = ClaudeBridgeListener(self.bot, self, bridge_cfg)
            await self._bridge.start()
        else:
            self._bridge = None

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self.bot.logger.log(MODULE_NAME, "HTTP debug server stopped")
        if self._bridge:
            await self._bridge.stop()


class ClaudeBridgeListener:
    """Polls EmbotDebug repo for commands, executes via RemoteDebugServer, commits results back."""

    # commands that return file artifacts committed to the repo
    FILE_COMMANDS = {"logs", "logs-list", "logs-search", "config", "db-query"}
    # commands blocked entirely
    BLOCKED = {"db-download", "stream"}

    def __init__(self, bot, server: "RemoteDebugServer", bridge_cfg: dict):
        self.bot = bot
        self.server = server
        self._cfg = bridge_cfg
        self._repo = bridge_cfg.get("repo", "Emball/EmbotDebug")
        self._token = bridge_cfg.get("token", "")
        self._interval = float(bridge_cfg.get("poll_interval", 2.0))
        self._last_seq = None
        self._task = None

    def _gh_request(self, path, method="GET", body=None):
        url = f"https://api.github.com/repos/{self._repo}/contents/{path}"
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"token {self._token}")
        req.add_header("Accept", "application/vnd.github.v3+json")
        if body:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        import ssl as _ssl
        ctx = _ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            return json.loads(r.read())

    def _gh_get_file(self, path):
        import base64
        data = self._gh_request(path)
        content = json.loads(base64.b64decode(data["content"]).decode())
        return content, data["sha"]

    def _gh_put_file(self, path, content, sha, message):
        import base64
        if isinstance(content, (dict, list)):
            raw = json.dumps(content, indent=2) + "\n"
        else:
            raw = str(content)
        body = {
            "message": message,
            "content": base64.b64encode(raw.encode()).decode(),
            "sha": sha,
        }
        self._gh_request(path, method="PUT", body=body)

    def _gh_put_binary(self, path, data: bytes, sha, message):
        import base64
        body = {
            "message": message,
            "content": base64.b64encode(data).decode(),
            "sha": sha,
        }
        self._gh_request(path, method="PUT", body=body)

    def _gh_get_sha(self, path):
        try:
            data = self._gh_request(path)
            return data["sha"]
        except Exception:
            return None

    async def _poll(self):
        while True:
            try:
                cmd, _ = await asyncio.get_event_loop().run_in_executor(
                    None, self._gh_get_file, "cmd.json"
                )
                seq = cmd.get("seq", 0)
                if seq != 0 and seq != self._last_seq:
                    command = cmd.get("command", "")
                    args = cmd.get("args", [])
                    self.bot.logger.log(MODULE_NAME, f"[bridge] seq={seq} cmd={command} args={args}")
                    output, artifacts = await self._execute(command, args)
                    await self._write_results(seq, command, output, artifacts)
                    self._last_seq = seq
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, f"[bridge] poll error", e)
            await asyncio.sleep(self._interval)

    async def _execute(self, command, args):
        """Route command to server handler, return (output_str, artifacts_dict)."""
        if command in self.BLOCKED:
            return f"command '{command}' not available via claude bridge", {}
        artifacts = {}

        if command == "ping":
            output = json.dumps({"ok": True, "time": datetime.now().isoformat()})

        elif command == "status":
            uptime = int(time.time() - self.server._start_time) if self.server._start_time else 0
            guilds = list(self.bot.guilds)
            output = json.dumps({
                "version": getattr(self.bot, "version", "unknown"),
                "latency": round(getattr(self.bot, "latency", 0) * 1000, 1),
                "guilds": len(guilds),
                "uptime_seconds": uptime,
                "user": str(self.bot.user) if self.bot.user else None,
                "log_file": str(self.bot.logger.log_file.name) if getattr(self.bot.logger, "log_file", None) else None,
            }, indent=2)

        elif command == "guilds":
            output = json.dumps([
                {"id": g.id, "name": g.name, "members": g.member_count,
                 "channels": len(g.channels), "roles": len(g.roles)}
                for g in self.bot.guilds
            ], indent=2)

        elif command == "modules":
            mods = []
            if hasattr(self.bot.logger, "log_file") and self.bot.logger.log_file.exists():
                with open(self.bot.logger.log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        m = re.search(r"Loaded module: (\S+)", line)
                        if m:
                            mods.append(m.group(1))
            output = json.dumps(mods, indent=2)

        elif command == "logs":
            if not hasattr(self.bot.logger, "log_file") or not self.bot.logger.log_file.exists():
                return "no log file", {}
            lines = int(args[0]) if args else 200
            with open(self.bot.logger.log_file, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
            content = "".join(all_lines[-lines:])
            artifacts["logs/current.log"] = content
            output = f"log committed ({min(lines, len(all_lines))} lines)"

        elif command == "logs-list":
            logs_dir = script_dir() / "logs"
            files = []
            if logs_dir.exists():
                for fp in sorted(logs_dir.glob("session_*.log"), key=lambda x: x.stat().st_mtime, reverse=True):
                    sc = sum(1 for l in open(fp, encoding="utf-8") if l.startswith("--- Session"))
                    files.append({"name": fp.name, "size": fp.stat().st_size, "sessions": sc})
            artifacts["logs/list.json"] = files
            output = f"{len(files)} log files"

        elif command == "logs-search":
            if not args:
                return "missing search pattern", {}
            pattern = args[0]
            max_r = int(args[1]) if len(args) > 1 else 100
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                return f"invalid regex: {e}", {}
            logs_dir = script_dir() / "logs"
            matches = []
            for fp in sorted(logs_dir.glob("session_*.log"), key=lambda x: x.stat().st_mtime, reverse=True):
                with open(fp, encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            matches.append({"file": fp.name, "line": i, "content": line.rstrip("\n")})
                            if len(matches) >= max_r:
                                break
                if len(matches) >= max_r:
                    break
            artifacts["logs/search.json"] = {"pattern": pattern, "matches": matches}
            output = f"{len(matches)} match(es)"

        elif command == "config":
            name = args[0] if args else ""
            if not name or name.lower() in ("auth", "token"):
                return "blocked or missing config name", {}
            cfg_path = script_dir() / "config" / f"{name}.json"
            if not cfg_path.exists():
                return f"config '{name}' not found", {}
            with open(cfg_path, encoding="utf-8") as f:
                data = json.load(f)
            artifacts[f"config/{name}.json"] = data
            output = f"config/{name}.json committed"

        elif command == "db-query":
            if len(args) < 2:
                return "usage: db-query <name> <sql>", {}
            name, query = args[0], " ".join(args[1:])
            db_path = script_dir() / "db" / f"{name}.db"
            if not db_path.exists():
                return f"db '{name}' not found", {}
            q_upper = query.strip().upper()
            if not (q_upper.startswith("SELECT") or q_upper.startswith("PRAGMA") or q_upper.startswith("EXPLAIN")):
                return "only SELECT/PRAGMA/EXPLAIN allowed", {}
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query)
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            artifacts[f"db/{name}_query.json"] = {"query": query, "rows": rows, "count": len(rows)}
            output = f"{len(rows)} row(s) — committed to db/{name}_query.json"

        elif command == "exec":
            cmd_str = " ".join(args) if args else ""
            if not cmd_str:
                return "missing command", {}
            proc = await asyncio.create_subprocess_shell(
                cmd_str, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
            except asyncio.TimeoutError:
                output = "timed out"

        elif command == "update":
            proc = await asyncio.create_subprocess_exec(
                "git", "-c", "credential.helper=", "pull", "--ff-only", "origin", "main",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
            )
            stdout, stderr = await proc.communicate()
            out = stdout.decode().strip() + "\n" + stderr.decode().strip()
            if proc.returncode == 0 and "Already up to date" not in out:
                asyncio.create_task(self._delayed_restart_with_log(artifacts))
                return out + "\n[restarting — log will be committed after startup]", {}
            output = out

        elif command == "restart":
            asyncio.create_task(self._delayed_restart_with_log(artifacts))
            output = "restarting — log will be committed after startup"

        else:
            output = f"unknown command: {command}"

        return output, artifacts

    async def _delayed_restart_with_log(self, artifacts):
        await asyncio.sleep(1)
        await self.bot.close()
        import os as _os, sys as _sys
        _os.execv(_sys.executable, [_sys.executable] + _sys.argv)

    async def _write_results(self, seq, command, output, artifacts):
        def _commit():
            # write result.json
            result_sha = self._gh_get_sha("result.json")
            self._gh_put_file("result.json", {"seq": seq, "command": command, "output": output}, result_sha or "", str(seq))
            # write artifacts
            for path, content in artifacts.items():
                sha = self._gh_get_sha(path)
                if isinstance(content, bytes):
                    self._gh_put_binary(path, content, sha or "", str(seq))
                elif isinstance(content, str):
                    import base64
                    body = {
                        "message": str(seq),
                        "content": base64.b64encode(content.encode()).decode(),
                    }
                    if sha:
                        body["sha"] = sha
                    self._gh_request(path, method="PUT", body=body)
                else:
                    self._gh_put_file(path, content, sha or "", str(seq))
        await asyncio.get_event_loop().run_in_executor(None, _commit)
        self.bot.logger.log(MODULE_NAME, f"[bridge] seq={seq} result committed")

    async def start(self):
        self._task = asyncio.create_task(self._poll())
        self.bot.logger.log(MODULE_NAME, f"[bridge] Claude bridge active — polling {self._repo}")

    async def stop(self):
        if self._task:
            self._task.cancel()


def setup(bot):
    _migrate_client_config()
    bot.logger.log(MODULE_NAME, "Setting up remote debug HTTP server")
    config = _load_config()
    if not config.get("server", False):
        bot.logger.log(MODULE_NAME, "Remote debug server disabled in config")
        return
    server = RemoteDebugServer(bot)
    bot.remote_debug_server = server

    async def _start():
        await bot.wait_until_ready()
        await server.start()

    bot._remote_debug_task = asyncio.create_task(_start())
    bot.logger.log(MODULE_NAME, "Remote debug module setup complete")


# ── Client mode (when run as `python modules/remote_debug.py <command>`) ──


def _client_url(cfg):
    url = cfg.get("url")
    if url:
        return url
    host = os.environ.get("REMOTE_HOST", cfg.get("host", "0.0.0.0"))
    port = os.environ.get("REMOTE_PORT", cfg.get("port", 8765))
    host = "127.0.0.1" if host == "0.0.0.0" else host
    return f"http://{host}:{port}"


def _load_client_config() -> dict:
    cfg = migrate_config(RD_CONFIG_PATH, RD_CONFIG_DEFAULTS)
    cfg["token"] = os.environ.get("REMOTE_TOKEN", cfg["token"])
    return cfg


def _client_request(cfg, path, raw=False, method="GET", data=None):
    url = _client_url(cfg).rstrip("/") + path
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


def _cmd_guilds(cfg):
    print(json.dumps(_client_request(cfg, "/guilds"), indent=2))


def _cmd_modules(cfg):
    print(json.dumps(_client_request(cfg, "/modules"), indent=2))


def _cmd_logs(cfg, lines=200, file=None, session=None):
    path = f"/logs?lines={lines}"
    if file:
        path += f"&file={urllib.parse.quote(file)}"
    if session:
        path += f"&session={urllib.parse.quote(str(session))}"
    data = _client_request(cfg, path, raw=True)
    sys.stdout.buffer.write(data + b"\n")


def _cmd_logs_list(cfg):
    print(json.dumps(_client_request(cfg, "/logs/list"), indent=2))


def _cmd_logs_search(cfg, query, max_results=100, max_files=0):
    path = f"/logs/search?q={urllib.parse.quote(query)}&max={max_results}"
    if max_files:
        path += f"&files={max_files}"
    result = _client_request(cfg, path)
    matches = result.get("matches", [])
    print(f"Found {result.get('total_matches', 0)} match(es) across {result.get('files_searched', 0)} file(s)")
    if result.get("truncated"):
        print("(results truncated — use --max for more)")
    print()
    for m in matches:
        print(f"  {m['file']}:{m['line']}  {m['content']}")


def _cmd_stream(cfg):
    url = _client_url(cfg).rstrip("/") + "/logs/stream"
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


def _wait_for_server(cfg, timeout=30):
    url = _client_url(cfg).rstrip("/") + "/ping"
    deadline = time.time() + timeout
    last_msg = 0
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url)
            if cfg["token"]:
                req.add_header("X-Debug-Token", cfg["token"])
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=3, context=ctx) as resp:
                if resp.status == 200:
                    print("Server is back online.")
                    return True
        except Exception:
            pass
        now = time.time()
        if now - last_msg >= 5:
            elapsed = int(now - (deadline - timeout))
            print(f"  Waiting {elapsed}s...")
            last_msg = now
        time.sleep(2)
    print("Timed out waiting for server.")
    return False


def _auto_tail(cfg, lines=30):
    time.sleep(8)
    sys.stdout.write(f"\n--- Last {lines} lines of startup log ---\n")
    sys.stdout.flush()
    _cmd_logs(cfg, lines=lines)


def _cmd_exec(cfg, cmd, timeout=15):
    if not cmd:
        cmd = sys.stdin.read().strip()
        if not cmd:
            print("No command provided. Pass as argument or pipe to stdin.")
            sys.exit(1)
    result = _client_request(cfg, "/exec", method="POST",
                             data=json.dumps({"cmd": cmd, "timeout": timeout}).encode())
    print(json.dumps(result, indent=2))


def _cmd_update(cfg):
    result = _client_request(cfg, "/update", method="POST")
    print(json.dumps(result, indent=2))
    if result.get("restarting"):
        _wait_for_server(cfg)
        _auto_tail(cfg)


def _cmd_restart(cfg):
    print(json.dumps(_client_request(cfg, "/restart", method="POST"), indent=2))
    _wait_for_server(cfg)
    _auto_tail(cfg)


def main():
    cfg = _load_client_config()
    if cfg.get("server", False):
        print("This config is set to server mode. Set \"server\": false to use the debug client, or point to a different config.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Embot remote debug client")
    parser.add_argument("--token", default=cfg["token"], help="Auth token")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="Test connection")
    sub.add_parser("status", help="Bot status")

    logs_parser = sub.add_parser("logs", help="Fetch recent logs")
    logs_parser.add_argument("--lines", type=int, default=200)
    logs_parser.add_argument("--file", default=None, help="Day log file (default: today)")
    logs_parser.add_argument("--session", default=None, help="Session number within day file (or 'all')")

    sub.add_parser("logs-list", help="List all log files")

    logs_search = sub.add_parser("logs-search", help="Search all log files")
    logs_search.add_argument("query", help="Search pattern (regex)")
    logs_search.add_argument("--max", type=int, default=100, help="Max results (default: 100)")
    logs_search.add_argument("--files", type=int, default=0, help="Max files to search (0=all)")

    sub.add_parser("guilds", help="List guilds with details")
    sub.add_parser("modules", help="List loaded module names")

    sub.add_parser("stream", help="Live log stream (Ctrl+C to stop)")

    db_dl = sub.add_parser("db-download", help="Download a database file")
    db_dl.add_argument("name", help="DB name (without .db)")

    db_q = sub.add_parser("db-query", help="Run a read-only SQL query")
    db_q.add_argument("name", help="DB name (without .db)")
    db_q.add_argument("query", help="SQL query (SELECT only)")

    config_p = sub.add_parser("config", help="View a config file")
    config_p.add_argument("name", help="Config name (without .json)")

    exec_p = sub.add_parser("exec", help="Run a shell command on the server")
    exec_p.add_argument("cmd", nargs="?", default=None, help="Command to run (omit to read from stdin)")
    exec_p.add_argument("--timeout", type=int, default=15, help="Timeout in seconds")

    sub.add_parser("update", help="Git pull and restart if updated")
    sub.add_parser("restart", help="Restart the bot")

    args = parser.parse_args()
    cfg["token"] = args.token

    if args.command == "ping":
        _cmd_ping(cfg)
    elif args.command == "status":
        _cmd_status(cfg)
    elif args.command == "guilds":
        _cmd_guilds(cfg)
    elif args.command == "modules":
        _cmd_modules(cfg)
    elif args.command == "logs":
        _cmd_logs(cfg, args.lines, args.file, args.session)
    elif args.command == "logs-list":
        _cmd_logs_list(cfg)
    elif args.command == "logs-search":
        _cmd_logs_search(cfg, args.query, args.max, args.files)
    elif args.command == "stream":
        _cmd_stream(cfg)
    elif args.command == "db-download":
        _cmd_db_download(cfg, args.name)
    elif args.command == "db-query":
        _cmd_db_query(cfg, args.name, args.query)
    elif args.command == "config":
        _cmd_config(cfg, args.name)
    elif args.command == "exec":
        _cmd_exec(cfg, args.cmd, args.timeout)
    elif args.command == "update":
        _cmd_update(cfg)
    elif args.command == "restart":
        _cmd_restart(cfg)


if __name__ == "__main__":
    main()
