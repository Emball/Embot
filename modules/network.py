"""
network.py — TCP network server/client for Embot instances.
Enables remote console control, file proxy, and session dominance.
"""
import asyncio
import json
import os
import sys
import time
import uuid
import socket
import traceback
from pathlib import Path
from datetime import datetime

MODULE_NAME = "NETWORK"

DEFAULT_PORT = 9876
HEARTBEAT_INTERVAL = 5.0
HEARTBEAT_TIMEOUT = 15.0

# ── Wire protocol helpers ─────────────────────────────────────────────────────

def _send(stream, obj):
    line = json.dumps(obj) + '\n'
    stream.write(line.encode())

async def _recv(reader):
    try:
        line = await reader.readline()
        if not line:
            return None
        return json.loads(line.decode())
    except Exception:
        return None

# ── NetworkServer ─────────────────────────────────────────────────────────────

class NetworkServer:
    def __init__(self, bot, host='0.0.0.0', port=DEFAULT_PORT):
        self.bot = bot
        self.host = host
        self.port = port
        self._server = None
        self._clients = {}          # client_id -> (reader, writer)
        self._console_clients = []  # writer streams that want console output
        self._session_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._paused = False
        self._paused_by = None

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        self.bot.logger.log(MODULE_NAME,
            f"Network server listening on {self.host}:{self.port} (session: {self._session_id})")

    async def stop(self):
        for _, (_, writer) in list(self._clients.items()):
            try:
                writer.close()
            except Exception:
                pass
        self._clients.clear()
        self._console_clients.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def broadcast_console(self, text):
        """Send console output to all subscribed clients."""
        dead = []
        for writer in self._console_clients:
            try:
                _send(writer, {"type": "console_output", "data": text})
            except Exception:
                dead.append(writer)
        for w in dead:
            try:
                self._console_clients.remove(w)
            except ValueError:
                pass

    def is_paused(self):
        return self._paused

    async def _handle_client(self, reader, writer):
        client_id = None
        try:
            hello = await _recv(reader)
            if not hello or hello.get("type") != "hello":
                writer.close()
                return

            client_id = hello.get("client_id", "unknown")
            mode = hello.get("mode", "")
            self._clients[client_id] = (reader, writer)

            addr = writer.get_extra_info('peername')
            self.bot.logger.log(MODULE_NAME,
                f"Client connected: {client_id} ({addr[0]}:{addr[1]}) mode={mode}")

            _send(writer, {
                "type": "hello_ack",
                "server_id": self._session_id,
                "version": getattr(self.bot, 'version', '0.0.0.0'),
                "paused": self._paused,
                "paused_by": self._paused_by,
            })

            while True:
                msg = await _recv(reader)
                if msg is None:
                    break
                await self._dispatch(client_id, writer, msg)

        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Client handler error", e)
        finally:
            if client_id:
                self._clients.pop(client_id, None)
                try:
                    self._console_clients.remove(writer)
                except ValueError:
                    pass
                self.bot.logger.log(MODULE_NAME, f"Client disconnected: {client_id}")

    async def _dispatch(self, client_id, writer, msg):
        msg_type = msg.get("type", "")

        if msg_type == "console_subscribe":
            if writer not in self._console_clients:
                self._console_clients.append(writer)
            _send(writer, {"type": "console_subscribed"})

        elif msg_type == "console_input":
            line = msg.get("data", "")
            _handle_console_input(self.bot, line)

        elif msg_type == "file_read":
            path = msg.get("path", "")
            try:
                full = _safe_path(path)
                if full and full.exists():
                    data = full.read_bytes()
                    _send(writer, {
                        "type": "file_data",
                        "path": path,
                        "data": data.hex(),
                        "exists": True,
                    })
                else:
                    _send(writer, {
                        "type": "file_data",
                        "path": path,
                        "data": None,
                        "exists": False,
                    })
            except Exception as e:
                _send(writer, {"type": "file_data", "path": path, "data": None, "exists": False, "error": str(e)})

        elif msg_type == "file_write":
            path = msg.get("path", "")
            data_hex = msg.get("data", "")
            try:
                full = _safe_path(path, write=True)
                if full:
                    full.parent.mkdir(parents=True, exist_ok=True)
                    full.write_bytes(bytes.fromhex(data_hex))
                    _send(writer, {"type": "file_write_ack", "path": path, "ok": True})
                else:
                    _send(writer, {"type": "file_write_ack", "path": path, "ok": False, "error": "invalid path"})
            except Exception as e:
                _send(writer, {"type": "file_write_ack", "path": path, "ok": False, "error": str(e)})

        elif msg_type == "file_list":
            path = msg.get("path", "")
            try:
                full = _safe_path(path)
                entries = []
                if full and full.exists() and full.is_dir():
                    for entry in full.iterdir():
                        entries.append({
                            "name": entry.name,
                            "is_dir": entry.is_dir(),
                            "size": entry.stat().st_size if entry.is_file() else 0,
                        })
                _send(writer, {"type": "file_list_result", "path": path, "entries": entries})
            except Exception as e:
                _send(writer, {"type": "file_list_result", "path": path, "entries": [], "error": str(e)})

        elif msg_type == "heartbeat":
            _send(writer, {"type": "heartbeat_ack"})

        elif msg_type == "dominance_claim":
            by_mode = msg.get("mode", "test")
            if by_mode == "test" and not self._paused:
                self._paused = True
                self._paused_by = client_id
                self.bot.logger.log(MODULE_NAME, f"Paused by {client_id} (test mode)")
                _send(writer, {"type": "dominance_ack", "paused": True})
                # Notify all other clients
                for cid, (_, cw) in list(self._clients.items()):
                    try:
                        _send(cw, {"type": "session_update", "action": "pause", "by": client_id})
                    except Exception:
                        pass

        elif msg_type == "dominance_release":
            if self._paused and self._paused_by == client_id:
                self._paused = False
                self._paused_by = None
                self.bot.logger.log(MODULE_NAME, f"Resumed (released by {client_id})")
                _send(writer, {"type": "dominance_ack", "paused": False})
                for cid, (_, cw) in list(self._clients.items()):
                    try:
                        _send(cw, {"type": "session_update", "action": "resume", "by": client_id})
                    except Exception:
                        pass


# ── NetworkClient ─────────────────────────────────────────────────────────────

class NetworkClient:
    def __init__(self, bot, host, port=DEFAULT_PORT):
        self.bot = bot
        self.host = host
        self.port = port
        self._reader = None
        self._writer = None
        self._client_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._console_output = asyncio.Queue()
        self._connected = False
        self._mode = ""

    @property
    def connected(self):
        return self._connected

    async def connect(self, mode="console"):
        self._mode = mode
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

        _send(self._writer, {
            "type": "hello",
            "client_id": self._client_id,
            "mode": mode,
            "version": getattr(self.bot, 'version', '0.0.0.0'),
        })

        resp = await _recv(self._reader)
        if not resp or resp.get("type") != "hello_ack":
            self._writer.close()
            raise ConnectionError("Server rejected connection")

        self._connected = True
        self.bot.logger.log(MODULE_NAME,
            f"Connected to {self.host}:{self.port} as {self._client_id} (server: {resp.get('server_id')})")
        return resp

    async def subscribe_console(self):
        _send(self._writer, {"type": "console_subscribe"})
        resp = await _recv(self._reader)
        return resp and resp.get("type") == "console_subscribed"

    async def send_console_input(self, line):
        _send(self._writer, {"type": "console_input", "data": line})

    async def read_file(self, path):
        _send(self._writer, {"type": "file_read", "path": path})
        resp = await _recv(self._reader)
        if resp and resp.get("exists") and resp.get("data"):
            return bytes.fromhex(resp["data"])
        return None

    async def write_file(self, path, data):
        _send(self._writer, {"type": "file_write", "path": path, "data": data.hex()})
        resp = await _recv(self._reader)
        return resp and resp.get("ok", False)

    async def list_files(self, path):
        _send(self._writer, {"type": "file_list", "path": path})
        resp = await _recv(self._reader)
        if resp:
            return resp.get("entries", [])
        return []

    async def claim_dominance(self, mode="test"):
        _send(self._writer, {"type": "dominance_claim", "mode": mode})
        return await _recv(self._reader)

    async def release_dominance(self):
        _send(self._writer, {"type": "dominance_release"})
        return await _recv(self._reader)

    async def recv(self):
        """Read next message from server."""
        return await _recv(self._reader)

    async def heartbeat_loop(self):
        while self._connected:
            try:
                _send(self._writer, {"type": "heartbeat"})
                await asyncio.sleep(HEARTBEAT_INTERVAL)
            except Exception:
                self._connected = False
                break

    async def close(self):
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass

# ── Helpers ────────────────────────────────────────────────────────────────────

def _script_root():
    return Path(__file__).parent.parent.absolute()

def _safe_path(rel_path, write=False):
    """Prevent directory traversal — restrict to project root."""
    root = _script_root()
    full = (root / rel_path).resolve()
    if not str(full).startswith(str(root)):
        return None
    return full


def _handle_console_input(bot, line):
    """Execute a console command on behalf of a remote client."""
    line = line.strip()
    if not line:
        return

    parts = line.split(maxsplit=1)
    command = parts[0].lower()
    args_str = parts[1] if len(parts) > 1 else ""

    if command == "help":
        # Capture print_help output
        import io as _io
        old_stdout = sys.stdout
        buf = _io.StringIO()
        sys.stdout = buf
        try:
            from Embot import print_help
            print_help()
        except Exception:
            pass
        sys.stdout = old_stdout
        output = buf.getvalue()
    elif command == "status":
        import io as _io
        old_stdout = sys.stdout
        buf = _io.StringIO()
        sys.stdout = buf
        try:
            from Embot import show_status
            show_status()
        except Exception:
            pass
        sys.stdout = old_stdout
        output = buf.getvalue()
    elif command == "version":
        import io as _io
        old_stdout = sys.stdout
        buf = _io.StringIO()
        sys.stdout = buf
        try:
            from Embot import show_version
            show_version()
        except Exception:
            pass
        sys.stdout = old_stdout
        output = buf.getvalue()
    elif command in getattr(bot, 'console_commands', {}):
        cmd_info = bot.console_commands[command]
        asyncio.run_coroutine_threadsafe(
            cmd_info['handler'](args_str), bot.loop
        )
        output = f"> {line}\n"
    elif command in ("exit", "quit"):
        output = "> exit (ignored — remote clients cannot shut down the server)\n"
    else:
        output = f"Unknown command: {command}. Type 'help' for available commands.\n"

    # Send output to all console subscribers
    if hasattr(bot, '_network_server') and bot._network_server:
        bot._network_server.broadcast_console(output)


# ── Console output hook ────────────────────────────────────────────────────────

class ConsoleHook:
    """Wraps ConsoleLogger to also broadcast to network clients."""
    def __init__(self, logger, network_server):
        self._logger = logger
        self._server = network_server
        self._original_write_to_file = logger._write_to_file
        self._original_log = logger.log

    def install(self):
        """Patch ConsoleLogger to also broadcast to network."""
        logger = self._logger
        server = self._server

        original_log = logger.log
        def hooked_log(module_name, message, level="INFO"):
            original_log(module_name, message, level)
            if server:
                ts = datetime.now().strftime("%H:%M:%S")
                server.broadcast_console(f"[{ts}] [{module_name}] [{level}] {message}\n")

        original_error = logger.error
        def hooked_error(module_name, message, exception=None):
            original_error(module_name, message, exception)
            if server:
                ts = datetime.now().strftime("%H:%M:%S")
                server.broadcast_console(f"[{ts}] [{module_name}] [ERROR] {message}\n")

        logger.log = hooked_log
        logger.error = hooked_error


def setup(bot):
    """Initialize network server (runs on primary instance). Skip if in client mode."""
    import sys as _sys
    if any(a in _sys.argv for a in ('--console', '-c', '--test', '-t')):
        return

    if not bot.config.get("network", {}).get("enabled", True):
        return

    port = bot.config.get("network", {}).get("port", DEFAULT_PORT)
    host = bot.config.get("network", {}).get("host", "0.0.0.0")

    server = NetworkServer(bot, host, port)
    bot._network_server = server

    # Start server and hook console output
    asyncio.create_task(server.start())

    hook = ConsoleHook(bot.logger, server)
    hook.install()

    bot.logger.log(MODULE_NAME, f"Network server started on {host}:{port}")
