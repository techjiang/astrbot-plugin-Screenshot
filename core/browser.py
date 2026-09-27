"""直连 CDP 的浏览器会话：不依赖 Playwright / Selenium 的 API 层。

只做三件事：拉起本地 Chromium、用 WebSocket 说 CDP、把画面截成 PNG。
所有命令都走 ``Page.*`` / ``Runtime.*`` / ``Emulation.*`` 这些底层域。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

logger = logging.getLogger("astrbot.screenshot")

# 容器里跑 Chromium 的常见姿势：禁沙箱 + 关 /dev/shm 依赖
DEFAULT_FLAGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--hide-scrollbars",
    "--mute-audio",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-extensions",
    "--disable-sync",
    "--disable-translate",
    "--disable-features=Translate,BackForwardCache",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--metrics-recording-only",
]

# 新版 Chromium 用 --headless=new，老版本只认 --headless，探测后择一
HEADLESS_FLAGS = ("--headless=new", "--headless")
# 系统里若没有中文字体，emoji 会渲染成豆腐块，这里兜一层
FONT_FALLBACK = (
    "Noto Color Emoji",
    "Noto Emoji",
    "Twemoji Mozilla",
    "Segoe UI Emoji",
    "Apple Color Emoji",
)

BINARY_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "msedge",
    "microsoft-edge",
    "headless_shell",
    "chrome-headless-shell",
)

BINARY_PATHS = (
    "/usr/local/bin/chromium",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/opt/google/chrome/chrome",
    "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

WINDOWS_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# 全局并发闸门：一个 Chromium 能同时服务的标签页有限，超过就排队
MAX_CONCURRENT_PAGES = 4


def find_browser(explicit: str = "") -> str:
    """定位 Chromium 可执行文件。

    探测顺序：显式配置 → PATH → 常见安装路径。找不到时给出可操作的错误信息。
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate)
        raise RuntimeError(
            f"配置的 browser_path 不存在：{explicit}。"
            "请填写 Chromium/Chrome 可执行文件的绝对路径。"
        )

    seen: set[str] = set()
    for name in BINARY_CANDIDATES + BINARY_PATHS:
        if name in seen:
            continue
        seen.add(name)
        if os.path.isabs(name):
            if Path(name).is_file():
                return name
        else:
            found = shutil.which(name)
            if found:
                return found

    if sys.platform.startswith("win"):
        for name in WINDOWS_CANDIDATES:
            if Path(name).is_file():
                return name

    raise RuntimeError(
        "未找到 Chromium/Chrome 可执行文件。"
        "Debian/Ubuntu 可执行 apt-get install -y chromium，"
        "或在插件配置中填写 browser_path。"
    )


async def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _translate_proxy(proxy: str) -> str:
    """给 Chromium 的 ``--proxy-server`` 补上 ``scheme://``，并保留账号密码。

    Chromium 的代理开关不带鉴权字段，所以这里把 ``user:pass@host`` 写回 URL，
    由 :meth:`CDPConnection.send` 在握手阶段补 ``Proxy-Authorization``。
    """
    proxy = proxy.strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = "http://" + proxy
    return proxy


def _proxy_of(proxy_url: str) -> tuple[str, dict[str, str]]:
    """把代理 URL 拆成 ``(--proxy-server 的取值, 额外请求头)``。"""
    server = _translate_proxy(proxy_url)
    if not server:
        return "", {}
    parts = urlsplit(server)
    if not parts.username:
        return server, {}
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    clean = urlunsplit((parts.scheme, host, "", "", ""))
    credentials = f"{parts.username}:{parts.password or ''}"
    token = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
    return clean, {"Proxy-Authorization": f"Basic {token}"}


class CDPError(RuntimeError):
    """CDP 返回的协议级错误。"""


class CDPConnection:
    """一条 CDP 连接的收发封装，按 id 匹配响应、按 method 分发事件。"""

    def __init__(self, session: aiohttp.ClientSession, ws_url: str) -> None:
        self._session = session
        self._ws_url = ws_url
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: dict[str, list[asyncio.Queue]] = {}
        self._reader: asyncio.Task | None = None
        self._closed = asyncio.Event()
        self.extra_headers: dict[str, str] = {}

    async def start(self) -> None:
        self._ws = await self._session.ws_connect(
            self._ws_url,
            max_msg_size=0,
            heartbeat=30,
            headers=self.extra_headers or {},
        )
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        self._closed.set()
        if self._reader and not self._reader.done():
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._fail_pending("CDP 连接已关闭")

    def _fail_pending(self, message: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(CDPError(message))
        self._pending.clear()

    async def send(self, method: str, timeout: float = 30.0, **params: Any) -> dict:
        if self._ws is None or self._ws.closed:
            raise CDPError("CDP 连接尚未建立或已断开")
        self._next_id += 1
        msg_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        payload = {"id": msg_id, "method": method, "params": params}
        try:
            await self._ws.send_json(payload)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise CDPError(f"{method} 超时（{timeout}s）") from exc
        except (aiohttp.ClientError, ConnectionError) as exc:
            self._pending.pop(msg_id, None)
            raise CDPError(f"{method} 发送失败：{exc}") from exc

    def subscribe(self, event: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._listeners.setdefault(event, []).append(queue)
        return queue

    def unsubscribe(self, event: str, queue: asyncio.Queue) -> None:
        listeners = self._listeners.get(event, [])
        if queue in listeners:
            listeners.remove(queue)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                # 大帧（长图）可能以分片形式到达，先拼回完整二进制
                if message.type is aiohttp.WSMsgType.BINARY:
                    message = await self._ws.receive()
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(message.data)
                except ValueError:
                    logger.debug("丢弃无法解析的 CDP 消息")
                    continue
                if "id" in data:
                    future = self._pending.pop(int(data["id"]), None)
                    if future and not future.done():
                        if "error" in data:
                            future.set_exception(
                                CDPError(str(data["error"].get("message", "未知 CDP 错误")))
                            )
                        else:
                            future.set_result(data.get("result", {}))
                else:
                    for queue in self._listeners.get(data.get("method", ""), []):
                        queue.put_nowait(data.get("params", {}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 连接断开时让所有等待者立刻失败，避免卡住
            logger.debug("CDP 读循环退出：%s", exc)
        finally:
            self._fail_pending("CDP 连接已断开")


class BrowserProcess:
    """一个受管的 Chromium 进程（remote-debugging-port 模式）。"""

    def __init__(
        self,
        binary: str,
        flags: list[str] | None = None,
        *,
        proxy: str = "",
    ) -> None:
        self.proxy = _translate_proxy(proxy)
        self.proxy_server, self.proxy_headers = _proxy_of(self.proxy)
        self.binary = binary
        self.flags = list(DEFAULT_FLAGS) + list(flags or [])
        self.port = 0
        self.user_agent = ""
        self._process: subprocess.Popen | None = None
        self._profile: tempfile.TemporaryDirectory | None = None

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        """拉起进程并等待调试端口就绪；headless 参数不兼容时自动回退。"""
        last_error: Exception | None = None
        for headless in HEADLESS_FLAGS:
            try:
                await self._spawn(headless)
                return
            except RuntimeError as exc:
                last_error = exc
                logger.debug("以 %s 启动失败：%s", headless, exc)
                await self._kill()
        raise RuntimeError(
            f"Chromium 启动失败：{last_error}。"
            "请确认浏览器依赖完整，或在配置中调整 launch_flags。"
        )

    def _command(self, headless: str) -> list[str]:
        command = [
            self.binary,
            headless,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self._profile.name if self._profile else ''}",
            "about:blank",
            *self.flags,
        ]
        if self.proxy_server:
            command.append(f"--proxy-server={self.proxy_server}")
        return command

    async def _spawn(self, headless: str) -> None:
        self.port = await _free_port()
        if self._profile is None:
            self._profile = tempfile.TemporaryDirectory(prefix="astrbot-shot-")
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._process = subprocess.Popen(
            self._command(headless),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=creation_flags,
        )
        await self._wait_ready()

    async def _kill(self) -> None:
        if self._process and self._process.poll() is None:
            with contextlib.suppress(Exception):
                self._process.kill()
                self._process.wait(timeout=5)
        self._process = None

    async def _wait_ready(self, timeout: float = 20.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with aiohttp.ClientSession() as session:
            while loop.time() < deadline:
                if self._process and self._process.poll() is not None:
                    raise RuntimeError("Chromium 启动后立即退出，请检查浏览器依赖是否完整")
                try:
                    async with session.get(f"{self.endpoint}/json/version") as resp:
                        if resp.status == 200:
                            info = await resp.json(content_type=None)
                            self.user_agent = info.get("User-Agent", "")
                            return
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    pass
                await asyncio.sleep(0.2)
        raise RuntimeError(f"等待 Chromium 调试端口超时（{timeout}s）")

    async def new_page(self, session: aiohttp.ClientSession) -> tuple[CDPConnection, str]:
        """新建标签页并连上它的 CDP，返回 ``(连接, targetId)``。"""
        request_kwargs: dict[str, Any] = {}
        if self.proxy_headers:
            request_kwargs["proxy"] = self.proxy
            request_kwargs["proxy_auth"] = aiohttp.BasicAuth(
                *self._proxy_credentials()
            )
        try:
            async with session.put(
                f"{self.endpoint}/json/new?about:blank", **request_kwargs
            ) as resp:
                if resp.status >= 400:
                    raise CDPError(f"新建标签页失败（HTTP {resp.status}）")
                info = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as exc:
            raise CDPError(f"新建标签页失败：{exc}") from exc

        conn = CDPConnection(session, info["webSocketDebuggerUrl"])
        conn.extra_headers.update(self.proxy_headers)
        await conn.start()
        return conn, info["id"]

    def _proxy_credentials(self) -> tuple[str, str]:
        parts = urlsplit(self.proxy)
        return parts.username or "", parts.password or ""

    async def close_page(
        self, session: aiohttp.ClientSession, target_id: str, conn: CDPConnection
    ) -> None:
        await conn.close()
        try:
            async with session.get(f"{self.endpoint}/json/close/{quote(target_id)}") as resp:
                await resp.read()
        except aiohttp.ClientError:
            pass

    async def stop(self) -> None:
        process = self._process
        if process and process.poll() is None:
            with contextlib.suppress(Exception):
                if os.name != "nt":
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                else:
                    process.terminate()
                await asyncio.get_running_loop().run_in_executor(
                    None, process.wait, 5
                )
            if process.poll() is None:  # 温柔不管用就硬来
                with contextlib.suppress(Exception):
                    if os.name != "nt":
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    else:
                        process.kill()
                    await asyncio.get_running_loop().run_in_executor(
                        None, process.wait, 3
                    )
        if self._profile:
            with contextlib.suppress(Exception):
                self._profile.cleanup()
        self._process = None
        self._profile = None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None


def decode_frame(result: dict) -> bytes:
    """把 ``Page.captureScreenshot`` 的返回解成 PNG 字节。"""
    data = result.get("data")
    if not data:
        raise CDPError("截图返回为空，可能是页面过大或渲染进程已崩溃")
    try:
        return base64.b64decode(data)
    except (ValueError, TypeError) as exc:
        raise CDPError("截图返回的 base64 数据损坏") from exc
