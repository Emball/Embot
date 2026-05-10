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
        import __main__
        data = __main__.get_modules_data()
        if data is None:
            return web.json_response({"error": "no log file"}, status=404)
        return web.json_response(data)

    def _logs_dir(self):
        return script_dir() / "logs"

    async def _handle_logs_list(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        import __main__
        data = __main__.get_logs_data(list_files=True)
        return web.json_response(data['files'])

    async def _handle_logs_search(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        query = request.query.get("q", "")
        if not query:
            return self._fail("missing query parameter 'q'", 400)
        try:
            max_results = min(int(request.query.get("max", "100")), 500)
        except ValueError:
            max_results = 100
        import __main__
        try:
            data = __main__.get_logs_data(search=query, search_max=max_results)
        except re.error as e:
            return self._fail(f"invalid regex: {e}", 400)
        return web.json_response({
            "query": data["query"],
            "matches": data["matches"],
            "total_matches": len(data["matches"]),
            "truncated": data["truncated"],
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
        query = request.query.get("q", "")
        if not query:
            return self._fail("missing query parameter 'q'", 400)
        import __main__
        rows, err = __main__.run_db_query(name, query)
        if err:
            status = 404 if "not found" in err else 403 if "only SELECT" in err or "invalid" in err else 500
            return web.json_response({"error": err}, status=status)
        return web.json_response({"rows": rows, "count": len(rows)})

    async def _handle_config(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        name = request.match_info["name"]
        if not self._sanitize_name(name):
            return self._fail("invalid config name", 400)
        import __main__
        data, err = __main__.get_config_data(name)
        if err:
            status = 403 if err == "access denied" else 404 if "not found" in err else 500
            return web.json_response({"error": err}, status=status)
        return web.json_response(data)

    async def _handle_update(self, request):
        if not self._check_auth(request) or not self._check_ip(request):
            return self._fail()
        import __main__
        try:
            if not __main__._ensure_git_for_update(self.bot, self.bot.logger):
                return web.json_response({"error": "git not available"}, status=500)
            updated = await __main__._check_for_update(self.bot)
            if updated:
                self.bot.logger.log(MODULE_NAME, "Update pulled — restarting")
                asyncio.create_task(self._delayed_restart())
                return web.json_response({"updated": True, "restarting": True})
            return web.json_response({"updated": False})
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
        import __main__
        try:
            out, err, code = await __main__.run_exec(cmd, timeout=timeout)
            return web.json_response({"exit_code": code, "stdout": out, "stderr": err})
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
            tok = self._config['token']
            self.bot.logger.log(MODULE_NAME, f"Auth token: {tok[:4]}{'*' * (len(tok) - 4)}")
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
    FILE_COMMANDS = {"logs", "config", "db-query"}
    # commands blocked entirely
    BLOCKED = {"db-download"}

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
        import time as _time
        bust = f"?t={int(_time.time()*1000)}" if method == "GET" else ""
        url = f"https://api.github.com/repos/{self._repo}/contents/{path}{bust}"
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"token {self._token}")
        req.add_header("Accept", "application/vnd.github.v3+json")
        req.add_header("Cache-Control", "no-cache")
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
        }
        if sha:
            body["sha"] = sha
        self._gh_request(path, method="PUT", body=body)

    def _gh_put_binary(self, path, data: bytes, sha, message):
        import base64
        body = {
            "message": message,
            "content": base64.b64encode(data).decode(),
        }
        if sha:
            body["sha"] = sha
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
                    self._last_seq = seq
                    output, artifacts = await self._execute(command, args)
                    asyncio.create_task(self._write_results(seq, command, output, artifacts))
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

        elif command == "modules":
            import __main__
            data = __main__.get_modules_data()
            output = json.dumps(data or {}, indent=2)

        elif command == "logs":
            import __main__
            # Parse flags: --tail N, --file NAME, --session ID, --list, --search PATTERN, --max N
            tail = 200; file = None; session = None; list_files = False; search = None; max_r = 100
            i = 0
            while i < len(args):
                if args[i] == "--tail" and i + 1 < len(args):
                    tail = int(args[i + 1]); i += 2
                elif args[i] == "--file" and i + 1 < len(args):
                    file = args[i + 1]; i += 2
                elif args[i] == "--session" and i + 1 < len(args):
                    session = args[i + 1]; i += 2
                elif args[i] == "--list":
                    list_files = True; i += 1
                elif args[i] == "--search" and i + 1 < len(args):
                    search = args[i + 1]; i += 2
                elif args[i] == "--max" and i + 1 < len(args):
                    max_r = int(args[i + 1]); i += 2
                elif args[i].lstrip('-').isdigit():
                    tail = int(args[i]); i += 1  # bare number = --tail N (legacy)
                else:
                    i += 1
            if list_files:
                data = __main__.get_logs_data(list_files=True)
                artifacts["logs/list.json"] = data['files']
                output = f"{len(data['files'])} log files"
            elif search:
                try:
                    data = __main__.get_logs_data(search=search, search_max=max_r)
                except re.error as e:
                    return f"invalid regex: {e}", {}
                artifacts["logs/search.json"] = {"pattern": search, "matches": data["matches"]}
                output = f"{len(data['matches'])} match(es)"
            else:
                data = __main__.get_logs_data(tail=tail, file=file, session=session)
                if data.get('error'):
                    return data['error'], {}
                content = data['lines']
                artifacts["logs/current.log"] = content
                output = f"log committed ({len(content.splitlines())} lines)"

        elif command == "config":
            name = args[0] if args else ""
            import __main__
            cfg_data, err = __main__.get_config_data(name)
            if err:
                return err, {}
            artifacts[f"config/{name}.json"] = cfg_data
            output = f"config/{name}.json committed"

        elif command == "db-query":
            if len(args) < 2:
                return "usage: db-query <name> <sql>", {}
            import __main__
            rows, err = __main__.run_db_query(args[0], " ".join(args[1:]))
            if err:
                return err, {}
            artifacts[f"db/{args[0]}_query.json"] = {"query": " ".join(args[1:]), "rows": rows, "count": len(rows)}
            output = f"{len(rows)} row(s) — committed to db/{args[0]}_query.json"

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
                asyncio.create_task(self._delayed_restart_with_log())
                return out + "\n[restarting — log will be committed after startup]", {}
            output = out

        elif command == "restart":
            asyncio.create_task(self._delayed_restart_with_log())
            output = "restarting — log will be committed after startup"

        else:
            output = f"unknown command: {command}"

        return output, artifacts

    async def _delayed_restart_with_log(self):
        await asyncio.sleep(1)
        await self.bot.close()
        import os as _os, sys as _sys
        _os.execv(_sys.executable, [_sys.executable] + _sys.argv)

    async def _write_results(self, seq, command, output, artifacts):
        def _commit():
            try:
                result_sha = self._gh_get_sha("result.json")
                self._gh_put_file("result.json", {"seq": seq, "command": command, "output": output}, result_sha or "", str(seq))
                for path, content in artifacts.items():
                    sha = self._gh_get_sha(path)
                    if isinstance(content, bytes):
                        self._gh_put_binary(path, content, sha or "", str(seq))
                    else:
                        self._gh_put_file(path, content, sha or "", str(seq))
                return None
            except Exception as e:
                import traceback as _tb
                return f"{e}\n{_tb.format_exc()}"
        err = await asyncio.get_event_loop().run_in_executor(None, _commit)
        if err:
            self.bot.logger.log(MODULE_NAME, f"[bridge] seq={seq} commit failed: {err}", "ERROR")
        else:
            self.bot.logger.log(MODULE_NAME, f"[bridge] seq={seq} result committed")

        await asyncio.sleep(15)

        def _zero():
            cmd_sha = self._gh_get_sha("cmd.json")
            result_sha2 = self._gh_get_sha("result.json")
            self._gh_put_file("cmd.json", {"seq": 0, "command": "", "args": []}, cmd_sha or "", "clear")
            self._gh_put_file("result.json", {"seq": 0, "command": "", "output": "", "error": ""}, result_sha2 or "", "clear")
        await asyncio.get_event_loop().run_in_executor(None, _zero)

    async def start(self):
        await asyncio.get_event_loop().run_in_executor(None, self._zero_on_start)
        self._task = asyncio.create_task(self._poll())
        self.bot.logger.log(MODULE_NAME, f"[bridge] Claude bridge active — polling {self._repo}")

    def _zero_on_start(self):
        cmd_sha = self._gh_get_sha("cmd.json")
        result_sha = self._gh_get_sha("result.json")
        self._gh_put_file("cmd.json", {"seq": 0, "command": "", "args": []}, cmd_sha or "", "clear")
        self._gh_put_file("result.json", {"seq": 0, "command": "", "output": "", "error": ""}, result_sha or "", "clear")

    async def stop(self):
        if self._task:
            self._task.cancel()


def setup(bot):
    _migrate_client_config()
    bot.logger.log(MODULE_NAME, "Setting up remote debug HTTP server")
    server = RemoteDebugServer(bot)
    bot.remote_debug_server = server

    async def _start():
        await bot.wait_until_ready()
        await server.start()

    bot._remote_debug_task = asyncio.create_task(_start())
    bot.logger.log(MODULE_NAME, "Remote debug module setup complete")


# ── Client mode (when run as `python modules/remote_debug.py <command>`) ──


def _client_url(cfg):
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


def _cmd_logs(cfg, lines=200, file=None, session=None, search=None, max_results=100):
    if search:
        path = f"/logs/search?q={urllib.parse.quote(search)}&max={max_results}"
        result = _client_request(cfg, path)
        matches = result.get("matches", [])
        print(f"Found {result.get('total_matches', 0)} match(es)")
        if result.get("truncated"):
            print("(results truncated — use --max for more)")
        print()
        for m in matches:
            print(f"  {m['file']}:{m['line']}  {m['content']}")
        return
    path = f"/logs?lines={lines}"
    if file:
        path += f"&file={urllib.parse.quote(file)}"
    if session:
        path += f"&session={urllib.parse.quote(str(session))}"
    data = _client_request(cfg, path, raw=True)
    sys.stdout.buffer.write(data + b"\n")


def _cmd_logs_list(cfg):
    print(json.dumps(_client_request(cfg, "/logs/list"), indent=2))



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


def _cmd_bridge(bridge_cfg, command, args, timeout=45):
    """Send a command via Claude bridge, wait for result, print everything. No external tools needed."""
    import base64 as _b64
    import tempfile, shutil

    repo = bridge_cfg.get("repo", "Emball/EmbotDebug")
    token = bridge_cfg.get("token", "")
    if not token:
        print("Error: claude_bridge.token not set in config", file=sys.stderr)
        sys.exit(1)

    def gh(path, method="GET", body=None):
        bust = f"?t={int(time.time()*1000)}" if method == "GET" else ""
        url = f"https://api.github.com/repos/{repo}/contents/{path}{bust}"
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"token {token}")
        req.add_header("Accept", "application/vnd.github.v3+json")
        req.add_header("Cache-Control", "no-cache")
        if body:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            return json.loads(r.read())

    def gh_get(path):
        data = gh(path)
        content = _b64.b64decode(data["content"]).decode()
        return json.loads(content), data["sha"]

    def gh_put(path, content, sha, message):
        if isinstance(content, (dict, list)):
            raw = json.dumps(content, indent=2) + "\n"
        else:
            raw = str(content)
        body = {"message": message, "content": _b64.b64encode(raw.encode()).decode()}
        if sha:
            body["sha"] = sha
        return gh(path, method="PUT", body=body)

    def gh_get_sha(path):
        try:
            return gh(path)["sha"]
        except Exception:
            return None

    def gh_get_text(path):
        """Get a file's raw text content."""
        try:
            data = gh(path)
            return _b64.b64decode(data["content"]).decode()
        except Exception:
            return None

    # get current seq
    try:
        cmd_data, _ = gh_get("cmd.json")
        seq = cmd_data.get("seq", 0) + 1
    except Exception as e:
        print(f"Error reading cmd.json: {e}", file=sys.stderr)
        sys.exit(1)

    new_cmd = {"seq": seq, "command": command, "args": args}

    # push with retry on conflict
    pushed = False
    for attempt in range(6):
        try:
            sha = gh_get_sha("cmd.json")
            gh_put("cmd.json", new_cmd, sha or "", str(seq))
            pushed = True
            break
        except urllib.error.HTTPError as e:
            if e.code == 409 and attempt < 5:
                time.sleep(1 + attempt)
                # re-read seq in case it changed
                try:
                    cmd_data, _ = gh_get("cmd.json")
                    seq = cmd_data.get("seq", 0) + 1
                    new_cmd["seq"] = seq
                except Exception:
                    pass
                continue
            print(f"Push failed: {e}", file=sys.stderr)
            sys.exit(1)

    if not pushed:
        print("Failed to push command after retries", file=sys.stderr)
        sys.exit(1)

    print(f"[bridge] sent seq={seq} cmd={command} args={args}", file=sys.stderr)

    # adaptive wait: restart/update take longer to come back
    initial_sleep = 8 if command in ("restart", "update") else 3
    time.sleep(initial_sleep)

    # poll for result
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result, _ = gh_get("result.json")
            if result.get("seq") == seq:
                output = result.get("output", "")
                error = result.get("error", "")

                # for file artifact commands, fetch the artifact too
                artifact_text = None
                if command == "logs" and "--list" not in args and "--search" not in args:
                    artifact_text = gh_get_text("logs/current.log")
                elif command == "logs" and "--search" in args:
                    artifact_text = gh_get_text("logs/search.json")
                elif command == "logs" and "--list" in args:
                    artifact_text = gh_get_text("logs/list.json")
                elif command == "config" and args:
                    artifact_text = gh_get_text(f"config/{args[0]}.json")
                elif command == "db-query" and args:
                    artifact_text = gh_get_text(f"db/{args[0]}_query.json")

                if artifact_text:
                    print(artifact_text)
                elif output:
                    print(output)
                if error:
                    print(f"Error: {error}", file=sys.stderr)
                return
        except Exception:
            pass
        time.sleep(2)

    print(f"[bridge] timed out waiting for seq={seq}", file=sys.stderr)
    sys.exit(1)


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
    logs_parser.add_argument("--tail", type=int, default=200)
    logs_parser.add_argument("--file", default=None, help="Day log file (default: today)")
    logs_parser.add_argument("--session", default=None, help="Session number within day file (or 'all')")
    logs_parser.add_argument("--search", default=None, help="Regex search across all log files")
    logs_parser.add_argument("--max", type=int, default=100, help="Max search results (default: 100)")

    sub.add_parser("logs-list", help="List all log files")

    sub.add_parser("guilds", help="List guilds with details")
    sub.add_parser("modules", help="List loaded module names")


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

    bridge_p = sub.add_parser("bridge", help="Send a command via Claude bridge and wait for result")
    bridge_p.add_argument("bridge_command", help="Command to send (e.g. status, logs, exec)")
    bridge_p.add_argument("bridge_args", nargs="*", help="Arguments for the command")
    bridge_p.add_argument("--timeout", type=int, default=45, help="Seconds to wait for result (default: 45)")

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
        _cmd_logs(cfg, args.tail, args.file, args.session, args.search, args.max)
    elif args.command == "logs-list":
        _cmd_logs_list(cfg)
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
    elif args.command == "bridge":
        bridge_cfg = cfg.get("claude_bridge", {}).copy()
        env_token = os.environ.get("EMBOT_BRIDGE_TOKEN", "")
        if env_token:
            bridge_cfg["token"] = env_token
        _cmd_bridge(bridge_cfg, args.bridge_command, args.bridge_args, args.timeout)


if __name__ == "__main__":
    main()
