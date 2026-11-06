"""联机：relay 控制面客户端 + frpc 进程管理。

流程：
1. 从主节点获取可用子节点列表；
2. 用房主的 Minecraft 令牌换取 relay 会话；
3. 开房 / 加入房间后拿到 frpc 参数，写好 TOML 并拉起 frpc；
4. STCP 模式下访客连接 ``127.0.0.1:<bind_port>`` 即可进入房间。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import net

FRP_VERSION = "0.72.0"

# 与 relay/signing.py 保持一致：服务端只接受启动器签名的请求。
APP_KEY = "aml-relay-2026-8f4b1c7d9e2a6350"


def signed_headers(method: str, path: str) -> dict[str, str]:
    """生成请求签名头，防止绕过启动器直接调用节点 API。"""
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(8)
    message = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}".encode("utf-8")
    signature = hmac.new(
        APP_KEY.encode("utf-8"), message, hashlib.sha256
    ).hexdigest()
    return {
        "X-AML-Ts": timestamp,
        "X-AML-Nonce": nonce,
        "X-AML-Sign": signature,
    }


DEFAULT_BIND_RANGE = (25600, 25999)


class RelayError(Exception):
    """联机服务端返回的错误或本地隧道故障。"""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ 控制面客户端
# 主节点地址不写入仓库，只随安装包分发：源码运行读项目根目录同名文件，
# 打包后读 PyInstaller 解包目录中的副本。
MASTER_ENDPOINT_FILE = "relay_endpoint.txt"


def _base(base: str) -> str:
    return str(base or "").strip().rstrip("/")


def master_endpoint() -> str:
    """返回内置的主节点地址；文件缺失时返回空串。"""
    root = getattr(sys, "_MEIPASS", None)
    base = Path(root) if root else Path(__file__).resolve().parents[1]
    try:
        text = (base / MASTER_ENDPOINT_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return _base(text.splitlines()[0] if text else "")


def fetch_nodes(master: str, *, timeout: int = 8) -> list[dict]:
    """查询主节点的子节点列表。"""
    master = _base(master)
    if not master:
        raise RelayError("未配置联机主节点地址")
    status, payload = net.request_json(
        f"{master}/api/v1/nodes",
        method="GET",
        timeout=timeout,
        headers=signed_headers("GET", "/api/v1/nodes"),
    )
    if status != 200 or not isinstance(payload.get("nodes"), list):
        raise RelayError(_error_message(payload, "获取联机节点失败"), code=str(status))
    return [item for item in payload["nodes"] if item.get("api")]


def _error_message(payload: dict, fallback: str) -> str:
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if isinstance(payload, dict) and payload.get("message"):
        return str(payload["message"])
    return fallback


class RelayClient:
    """单个子节点的 API 客户端。"""

    def __init__(self, api: str, *, token: str = "", timeout: int = 15) -> None:
        self.api = _base(api)
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        headers = signed_headers(method, path)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        status, payload = net.request_json(
            f"{self.api}{path}",
            method=method,
            json_body=body,
            headers=headers,
            timeout=self.timeout,
        )
        if status >= 400:
            raise RelayError(_error_message(payload, "联机服务请求失败"), code=str(status))
        return payload if isinstance(payload, dict) else {}

    def meta(self) -> dict:
        return self._call("GET", "/api/v1/meta")

    def session(self, name: str = "") -> dict:
        return self._call("POST", "/api/v1/auth/session", {"name": name})

    def create_room(self, payload: dict) -> dict:
        return self._call("POST", "/api/v1/rooms", payload)

    def lookup(self, code: str) -> dict:
        return self._call("POST", "/api/v1/rooms/lookup", {"code": code})

    def list_rooms(self) -> list[dict]:
        payload = self._call("GET", "/api/v1/rooms")
        rooms = payload.get("rooms")
        return rooms if isinstance(rooms, list) else []

    def room_info(self, room_id: str) -> dict:
        return self._call("GET", f"/api/v1/rooms/{room_id}/info")

    def join(self, room_id: str, *, bind_port: int = 0) -> dict:
        body: dict = {}
        if int(bind_port or 0) > 0:
            body["bind_port"] = int(bind_port)
        return self._call("POST", f"/api/v1/rooms/{room_id}/join", body)

    def heartbeat(self, room_id: str) -> dict:
        return self._call("POST", f"/api/v1/rooms/{room_id}/heartbeat")

    def leave(self, room_id: str) -> dict:
        return self._call("POST", f"/api/v1/rooms/{room_id}/leave")

    def close(self, room_id: str) -> dict:
        return self._call("DELETE", f"/api/v1/rooms/{room_id}")

    def detail(self, room_id: str) -> dict:
        return self._call("GET", f"/api/v1/rooms/{room_id}")


# ------------------------------------------------------------------ frpc 管理
def _platform_target() -> tuple[str, str]:
    """返回 (release 资产后缀, 压缩包内可执行文件名)。"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    if system == "windows":
        return f"windows_{arch}.zip", "frpc.exe"
    if system == "darwin":
        return f"darwin_{arch}.tar.gz", "frpc"
    return f"linux_{arch}.tar.gz", "frpc"


def frpc_path(tools_dir: Path) -> Path:
    target, exe_name = _platform_target()
    return Path(tools_dir) / f"frp-{FRP_VERSION}" / exe_name


def bundled_frpc_path() -> Optional[Path]:
    """返回随启动器分发的 frpc（打包后位于 PyInstaller 的 _MEIPASS 根目录）。"""
    _, exe_name = _platform_target()
    root = getattr(sys, "_MEIPASS", None)
    base = Path(root) if root else Path(__file__).resolve().parents[1]
    candidate = base / exe_name
    if candidate.is_file() and candidate.stat().st_size > 1024:
        return candidate
    return None


def ensure_frpc(tools_dir: Path) -> Path:
    """确保 frpc 就绪：从安装包内置的二进制复制，不联网下载。"""
    exe = frpc_path(tools_dir)
    if exe.is_file() and exe.stat().st_size > 1024:
        return exe

    source = bundled_frpc_path()
    if source is None:
        raise RelayError("安装包缺少内置 frpc，请重新下载完整安装包")
    exe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, exe)
    if os.name != "nt":
        exe.chmod(0o755)
    if not exe.is_file() or exe.stat().st_size <= 1024:
        raise RelayError("内置 frpc 复制失败")
    return exe


def _q(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def frpc_config_text(frpc: dict, *, kind: str) -> str:
    """把服务端下发的参数转成 frpc TOML。"""
    server_addr = frpc.get("server_addr") or frpc.get("serverAddr") or ""
    server_port = int(frpc.get("server_port") or frpc.get("serverPort") or 3300)
    auth_token = frpc.get("auth_token") or frpc.get("authToken") or ""
    tls = bool(frpc.get("tls", True))
    proxy = frpc.get("proxy") or {}
    lines = [
        f"serverAddr = {_q(server_addr)}",
        f"serverPort = {server_port}",
        f"transport.tls.enable = {'true' if tls else 'false'}",
    ]
    if auth_token:
        lines += ['auth.method = "token"', f"auth.token = {_q(auth_token)}"]

    if kind == "host":
        lines += [
            "",
            "[[proxies]]",
            f"name = {_q(proxy.get('name') or 'aml_host')}",
            f'type = {_q(proxy.get("type") or "stcp")}',
            f"secretKey = {_q(proxy.get('secret_key') or proxy.get('secretKey') or '')}",
            f"localIP = {_q(proxy.get('local_ip') or '127.0.0.1')}",
            f"localPort = {int(proxy.get('local_port') or 25565)}",
        ]
    else:
        lines += [
            "",
            "[[visitors]]",
            f"name = {_q(proxy.get('name') or 'aml_visitor')}",
            f'type = {_q(proxy.get("type") or "stcp")}',
            f"serverName = {_q(proxy.get('server_name') or proxy.get('serverName') or '')}",
            f"secretKey = {_q(proxy.get('secret_key') or proxy.get('secretKey') or '')}",
            f"bindAddr = {_q(proxy.get('bind_addr') or '127.0.0.1')}",
            f"bindPort = {int(proxy.get('bind_port') or proxy.get('bindPort') or 25599)}",
        ]
    return "\n".join(lines) + "\n"


def detect_lan_port(log_path: Optional[Path], *, default: int = 25565) -> int:
    """从 latest.log 里找“对局域网开放”的端口。"""
    if not log_path:
        return default
    path = Path(log_path)
    if not path.is_file():
        return default
    try:
        # latest.log 可能很大，只读最后 512KB 足够覆盖最近的开放记录。
        with open(path, "rb") as handle:
            handle.seek(max(0, path.stat().st_size - 512 * 1024))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return default
    patterns = [
        r"Local game hosted on port (\d+)",
        r"Started serving on (\d+)",
        r"LAN server (?:running|started) on (?:port )?(\d+)",
    ]
    port = 0
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            port = int(match.group(1))
    return port or default


def pick_free_port(start: int = DEFAULT_BIND_RANGE[0], end: int = DEFAULT_BIND_RANGE[1]) -> int:
    """在 127.0.0.1 上找一个空闲端口，供访客端 visitor 使用。"""
    import socket

    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RelayError("找不到可用的本地端口")


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        piece = value & 0x7F
        value >>= 7
        if value:
            out.append(piece | 0x80)
        else:
            out.append(piece)
            return bytes(out)


def probe_minecraft(port: int, *, timeout: float = 0.8, host: str = "127.0.0.1") -> bool:
    """向本地端口发送 Minecraft 状态握手，确认它是游戏服务而不是其他服务。

    这是“防止把任意 TCP 服务打进隧道”的第一道本地校验；探测失败就不允许开房。
    """
    import socket

    try:
        port = int(port)
        if not (1024 <= port <= 65535):
            return False
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            # 状态握手：packetId=0, protocol=0, address, port, nextState=1
            handshake = (
                _varint(0)
                + _varint(0)
                + _varint(len(host.encode("utf-8")))
                + host.encode("utf-8")
                + int(port).to_bytes(2, "big")
                + _varint(1)
            )
            sock.sendall(_varint(len(handshake)) + handshake)
            # 状态请求：packetId=0
            sock.sendall(b"\x01\x00")
            chunks = []
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data = sock.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
                raw = b"".join(chunks)
                if b'"version"' in raw or b'"description"' in raw:
                    return True
            raw = b"".join(chunks)
            return b'"version"' in raw or b'"description"' in raw
    except (OSError, ValueError):
        return False


def list_listening_ports() -> list[int]:
    """列出本机所有 TCP LISTENING 端口（用于发现已开放的局域网游戏）。"""
    ports: set[int] = set()
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
            for line in (result.stdout or "").splitlines():
                parts = line.split()
                if len(parts) < 4 or parts[0].upper() != "TCP":
                    continue
                if parts[3].upper() != "LISTENING":
                    continue
                local = parts[1]
                if ":" not in local:
                    continue
                try:
                    ports.add(int(local.rsplit(":", 1)[-1]))
                except ValueError:
                    continue
        else:
            result = subprocess.run(
                ["ss", "-ltn"], capture_output=True, text=True, timeout=8
            )
            for line in (result.stdout or "").splitlines():
                parts = line.split()
                if len(parts) < 4 or not parts[0].lower().startswith("tcp"):
                    continue
                local = parts[3]
                try:
                    ports.add(int(local.rsplit(":", 1)[-1]))
                except ValueError:
                    continue
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(port for port in ports if 1024 <= port <= 65535)


def scan_minecraft_ports(
    log_port: Optional[int] = None, *, timeout: float = 0.75, workers: int = 16
) -> list[dict]:
    """扫描并校验本机 Minecraft 监听端口。"""
    candidates = set(list_listening_ports())
    if log_port and int(log_port) > 0:
        candidates.add(int(log_port))
    if not candidates:
        return []
    from concurrent.futures import ThreadPoolExecutor

    def check(port: int) -> dict:
        return {"port": port, "minecraft": probe_minecraft(port, timeout=timeout)}

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(candidates)))) as pool:
        results = list(pool.map(check, sorted(candidates)))
    results.sort(key=lambda item: (not item["minecraft"], item["port"]))
    return results


def version_tuple(text: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
    if not match:
        return (0, 0, 0)
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def launch_connect_args(game_version: str, port: int) -> list[str]:
    """生成“启动后自动进入服务器”的参数（只连接本机回环地址）。"""
    address = f"127.0.0.1:{int(port)}"
    if version_tuple(game_version) >= (1, 20, 0):
        return ["--quickPlayMultiplayer", address]
    # 1.20 之前没有 quickPlay，但旧版本支持 --server/--port。
    return ["--server", "127.0.0.1", "--port", str(int(port))]


def supports_auto_connect(game_version: str) -> bool:
    return version_tuple(game_version) >= (1, 20, 0)


@dataclass
class Tunnel:
    """一次 frpc 运行实例。"""

    kind: str
    name: str
    config_path: Path
    bind_port: int = 0
    process: Optional[subprocess.Popen] = None
    started_at: float = field(default_factory=time.time)
    logs: deque = field(default_factory=lambda: deque(maxlen=200))

    @property
    def running(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def tail(self, limit: int = 12) -> list[str]:
        return list(self.logs)[-limit:]


class TunnelRunner:
    """同一时间只维护一条隧道（房主或访客）。"""

    def __init__(
        self,
        tools_dir: Path,
        work_dir: Path,
        *,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.tools_dir = Path(tools_dir)
        self.work_dir = Path(work_dir)
        self.on_log = on_log
        self.tunnel: Optional[Tunnel] = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self.tunnel and self.tunnel.running)

    def start(
        self,
        kind: str,
        name: str,
        frpc: dict,
        *,
        bind_port: int = 0,
        timeout: float = 60.0,
    ) -> Tunnel:
        self.stop()
        exe = ensure_frpc(self.tools_dir)
        room_dir = self.work_dir / name
        room_dir.mkdir(parents=True, exist_ok=True)
        config_path = room_dir / "frpc.toml"
        config_path.write_text(frpc_config_text(frpc, kind=kind), encoding="utf-8")

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        process = subprocess.Popen(
            [str(exe), "-c", str(config_path)],
            cwd=str(room_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        tunnel = Tunnel(
            kind=kind,
            name=name,
            config_path=config_path,
            bind_port=int(bind_port or 0),
            process=process,
        )
        threading.Thread(
            target=self._pump, args=(tunnel,), daemon=True, name="aml-frpc-log"
        ).start()
        with self._lock:
            self.tunnel = tunnel

        # 给 frpc 一点启动时间，尽早暴露端口占用 / 配置错误。
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                detail = "\n".join(tunnel.tail(6)) or "无输出"
                raise RelayError(f"frpc 启动失败（退出码 {process.returncode}）：\n{detail}")
            time.sleep(0.1)
        return tunnel

    def _pump(self, tunnel: Tunnel) -> None:
        process = tunnel.process
        if not process or not process.stdout:
            return
        for line in process.stdout:
            text = line.rstrip()
            if not text:
                continue
            tunnel.logs.append(text)
            if self.on_log:
                try:
                    self.on_log(text)
                except Exception:  # noqa: BLE001 - 日志回调不应影响隧道
                    pass

    def stop(self) -> None:
        with self._lock:
            tunnel, self.tunnel = self.tunnel, None
        if not tunnel or not tunnel.process:
            return
        process = tunnel.process
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:  # noqa: BLE001 - 强杀兜底
                try:
                    process.kill()
                except Exception:  # noqa: BLE001
                    pass

    def status(self) -> dict:
        with self._lock:
            tunnel = self.tunnel
        if not tunnel:
            return {"running": False}
        return {
            "running": tunnel.running,
            "kind": tunnel.kind,
            "name": tunnel.name,
            "bind_port": tunnel.bind_port,
            "uptime": int(time.time() - tunnel.started_at),
            "logs": tunnel.tail(12),
        }
