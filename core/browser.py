"""直连 CDP 的浏览器会话：不依赖 Playwright / Selenium 的 API 层。

只做三件事：拉起本地 Chromium、用 WebSocket 说 CDP、把画面截成 PNG。
所有命令都走 ``Page.*`` / ``Runtime.*`` / ``Emulation.*`` 这些底层域。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger("astrbot.screenshot")

# 容器里跑 Chromium 的常见姿势：禁沙箱 + 关 /dev/shm 依赖
DEFAULT_FLAGS = [
    "--headless=new",
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
    "--metrics-recording-only",
]

# 新版 Chromium 用 --headless=new，老版本只认 --headless，探测后择一
HEADLESS_FLAGS = ("--headless=new", "--headless")

BINARY_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "headless_shell",
    "/usr/local/bin/chromium",
    "/usr/bin/chromium",
    "/opt/google/chrome/chrome",
)


def find_browser(explicit: str = "") -> str:
    """定位 Chromium 可执行文件。"""
    if explicit:
        return explicit
    for name in BINARY_CANDIDATES:
        if os.path.isabs(name):
            if Path(name).is_file():
                return name
        else:
            found = shutil.which(name)
            if found:
                return found
    raise RuntimeError(
        "未找到 Chromium/Chrome，可执行文件不存在。"
        "请安装 chromium 或在配置中填写 browser_path。"
    )


async def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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

    async def start(self) -> None:
        self._ws = await self._session.ws_connect(self._ws_url, max_msg_size=0)
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def send(self, method: str, timeout: float = 30.0, **params: Any) -> dict:
        if self._ws is None:
            raise CDPError("连接尚未建立")
        self._next_id += 1
        msg_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        payload = {"id": msg_id, "method": method, "params": params}
        await self._ws.send_json(payload)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise CDPError(f"{method} 超时（{timeout}s）") from exc

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
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                if "id" in data:
                    future = self._pending.pop(int(data["id"]), None)
                    if future and not future.done():
                        if "error" in data:
                            future.set_exception(
                                CDPError(f"{data['error'].get('message')}")
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
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CDPError("CDP 连接已断开"))
            self._pending.clear()


class BrowserProcess:
    """一个受管的 Chromium 进程（remote-debugging-port 模式）。"""

    def __init__(self, binary: str, flags: list[str] | None = None) -> None:
        self.binary = binary
        self.flags = list(DEFAULT_FLAGS) + list(flags or [])
        self.port = 0
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

    async def _spawn(self, headless: str) -> None:
        self.port = await _free_port()
        if self._profile is None:
            self._profile = tempfile.TemporaryDirectory(prefix="astrbot-shot-")
        self._process = subprocess.Popen(
            [
                self.binary,
                headless,
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self._profile.name}",
                "about:blank",
                *self.flags,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await self._wait_ready()

    async def _kill(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.kill()
            self._process.wait()
        self._process = None

    async def _wait_ready(self, timeout: float = 20.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        async with aiohttp.ClientSession() as session:
            while asyncio.get_running_loop().time() < deadline:
                if self._process and self._process.poll() is not None:
                    raise RuntimeError("Chromium 启动后立即退出，请检查浏览器依赖是否完整")
                try:
                    async with session.get(f"{self.endpoint}/json/version") as resp:
                        if resp.status == 200:
                            return
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.2)
        raise RuntimeError(f"等待 Chromium 调试端口超时（{timeout}s）")

    async def new_page(self, session: aiohttp.ClientSession) -> tuple[CDPConnection, str]:
        """新建标签页并连上它的 CDP，返回 ``(连接, targetId)``。"""
        async with session.put(
            f"{self.endpoint}/json/new?about:blank"
        ) as resp:
            info = await resp.json(content_type=None)
        conn = CDPConnection(session, info["webSocketDebuggerUrl"])
        await conn.start()
        return conn, info["id"]

    async def close_page(
        self, session: aiohttp.ClientSession, target_id: str, conn: CDPConnection
    ) -> None:
        await conn.close()
        try:
            async with session.get(f"{self.endpoint}/json/close/{target_id}") as resp:
                await resp.read()
        except aiohttp.ClientError:
            pass

    async def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._process.wait, 5
                )
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._profile:
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
        raise CDPError("截图返回为空")
    return base64.b64decode(data)
