"""把 ShotOptions 落到浏览器上：导航、等渲染、按模式截图。"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from .browser import BrowserProcess, CDPConnection, CDPError, decode_frame
from .config import ShotOptions, viewport_for

logger = logging.getLogger("astrbot.screenshot")

MAX_CAPTURE_HEIGHT = 20000  # 超过这个高度的整页图会被裁掉，防止 CDP 返回超大帧
NETWORK_IDLE_MS = 800


class ScreenshotError(RuntimeError):
    """截图过程中的可预期失败。"""


class ScreenshotSession:
    """常驻一个 Chromium 进程，每次截图开一个标签页，用完即关。"""

    def __init__(self, binary: str, flags: list[str] | None = None) -> None:
        self.binary = binary
        self.flags = flags
        self._process: BrowserProcess | None = None
        self._http: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def ensure_started(self) -> None:
        async with self._lock:
            if self._process and self._process.alive:
                return
            if self._process:
                await self._process.stop()
            process = BrowserProcess(self.binary, self.flags)
            await process.start()
            if self._http is None or self._http.closed:
                self._http = aiohttp.ClientSession()
            self._process = process
            logger.info("Chromium 已就绪（端口 %s）", process.port)

    async def shutdown(self) -> None:
        async with self._lock:
            if self._process:
                await self._process.stop()
                self._process = None
            if self._http and not self._http.closed:
                await self._http.close()
            self._http = None

    async def capture(self, opts: ShotOptions) -> bytes:
        await self.ensure_started()
        assert self._process is not None and self._http is not None

        conn, target_id = await self._process.new_page(self._http)
        try:
            return await self._render(conn, opts)
        finally:
            await self._process.close_page(self._http, target_id, conn)

    async def _render(self, conn: CDPConnection, opts: ShotOptions) -> bytes:
        (width, height), preset_dpr, preset_mobile = viewport_for(opts.device)
        dpr = preset_dpr * opts.scale
        mobile = preset_mobile if opts.mobile is None else opts.mobile
        timeout = max(opts.timeout_ms, 1000) / 1000

        await conn.send("Page.enable")
        await conn.send("Runtime.enable")
        await conn.send("Emulation.setDeviceMetricsOverride",
                        width=width, height=height,
                        deviceScaleFactor=dpr, mobile=mobile)
        await conn.send(
            "Emulation.setEmulatedMedia",
            features=[{"name": "prefers-color-scheme",
                       "value": "dark" if opts.dark else "light"}],
        )

        await self._navigate(conn, opts, timeout)

        if opts.wait_for:
            await self._wait_for_selector(conn, opts.wait_for, timeout)
        if opts.wait_ms:
            await asyncio.sleep(min(opts.wait_ms, 30000) / 1000)
        if opts.hide:
            await self._apply_hide(conn, opts.hide)
        if opts.watermark:
            await self._apply_watermark(conn, opts.watermark)

        return await self._capture_by_mode(conn, opts)

    async def _navigate(
        self, conn: CDPConnection, opts: ShotOptions, timeout: float
    ) -> None:
        loaded = conn.subscribe("Page.loadEventFired")
        network = conn.subscribe("Network.loadingFinished")
        try:
            await conn.send("Network.enable")
            await conn.send(
                "Page.navigate", url=opts.url
            )

            # 先等 load 事件，拿不到就退化成「等到没有新请求」
            try:
                await asyncio.wait_for(loaded.get(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.debug("等待 load 事件超时，继续按网络空闲判断")

            await self._drain_network(network, timeout)
        finally:
            for event, queue in (
                ("Page.loadEventFired", loaded),
                ("Network.loadingFinished", network),
            ):
                conn.unsubscribe(event, queue)

        await conn.send("Runtime.evaluate", expression=(
            "document.readyState === 'complete' || document.fonts.ready.then(()=>1)"
        ), awaitPromise=True)

    async def _drain_network(self, network: asyncio.Queue, timeout: float) -> None:
        """等网络静默：连续 NETWORK_IDLE_MS 没有完成的请求即视为空闲。"""
        idle = NETWORK_IDLE_MS / 1000
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(network.get(), timeout=min(idle, remaining))
            except asyncio.TimeoutError:
                return

    async def _wait_for_selector(
        self, conn: CDPConnection, selector: str, timeout: float
    ) -> None:
        expression = (
            "(() => { try { return !!document.querySelector(%s) } "
            "catch (e) { return 'invalid' } })()"
        ) % _js_string(selector)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            result = await conn.send("Runtime.evaluate", expression=expression,
                                     returnByValue=True)
            value = result.get("result", {}).get("value")
            if value == "invalid":
                raise ScreenshotError(f"选择器不合法：{selector}")
            if value is True:
                return
            await asyncio.sleep(0.15)
        raise ScreenshotError(f"等待元素超时：{selector}")

    async def _apply_hide(self, conn: CDPConnection, selectors: list[str]) -> None:
        """隐藏干扰元素；选择器写错只跳过，不影响整张截图。"""
        for selector in selectors:
            await conn.send("Runtime.evaluate", expression=(
                "(() => { try { document.querySelectorAll(%s).forEach("
                "el => el.style.setProperty('display','none','important')) } "
                "catch (e) {} })()"
            ) % _js_string(selector))

    async def _apply_watermark(self, conn: CDPConnection, text: str) -> None:
        await conn.send("Runtime.evaluate", expression=(
            "(() => {"
            "  const id = '__astrbot_shot_mark__';"
            "  if (document.getElementById(id)) return;"
            "  const box = document.createElement('div');"
            "  box.id = id;"
            "  box.textContent = %s;"
            "  box.style.cssText = 'position:fixed;right:12px;bottom:10px;z-index:2147483647;"
            "font:12px/1.6 sans-serif;color:#fff;background:rgba(0,0,0,.45);"
            "padding:2px 10px;border-radius:10px;pointer-events:none';"
            "  (document.body || document.documentElement).appendChild(box);"
            "})()"
        ) % _js_string(text))

    async def _capture_by_mode(self, conn: CDPConnection, opts: ShotOptions) -> bytes:
        if opts.mode == "element":
            return await self._capture_element(conn, opts)

        full_page = opts.mode == "full"
        clip = None
        if full_page:
            metrics = await conn.send("Page.getLayoutMetrics")
            css = metrics.get("cssContentSize") or metrics.get("contentSize") or {}  # cssContentSize 更贴近视觉尺寸
            height = min(int(css.get("height", 0)) or 0, MAX_CAPTURE_HEIGHT)
            width = int(css.get("width", 0)) or 0
            if height and width:
                clip = {"x": 0, "y": 0, "width": width, "height": height, "scale": 1}
            else:
                full_page = False

        result = await conn.send(
            "Page.captureScreenshot",
            format="png",
            captureBeyondViewport=bool(full_page),
            fromSurface=True,
            **({"clip": clip} if clip else {}),
        )
        return decode_frame(result)

    async def _capture_element(self, conn: CDPConnection, opts: ShotOptions) -> bytes:
        expression = (
            "(() => { try { return document.querySelector(%s) } "
            "catch (e) { return null } })()"
        ) % _js_string(opts.selector)
        result = await conn.send("Runtime.evaluate", expression=expression)
        object_id = result.get("result", {}).get("objectId")
        if not object_id:
            raise ScreenshotError(f"页面中没有找到元素：{opts.selector}")

        try:
            box = await conn.send("DOM.getBoxModel", timeout=10, objectId=object_id)
        except CDPError as exc:
            raise ScreenshotError(f"元素不可见，无法截图：{opts.selector}") from exc

        quad = box["model"]["border"]
        left, top = quad[0], quad[1]
        right, bottom = quad[4], quad[5]
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            raise ScreenshotError(f"元素尺寸为 0，无法截图：{opts.selector}")

        result = await conn.send(
            "Page.captureScreenshot",
            format="png",
            captureBeyondViewport=True,
            fromSurface=True,
            clip={"x": left, "y": top, "width": width, "height": height, "scale": 1},
        )
        return decode_frame(result)

    async def render_html(self, html: str, opts: ShotOptions) -> bytes:
        """把一段 HTML 直接渲染成图，不经过网络。"""
        await self.ensure_started()
        assert self._process is not None and self._http is not None
        conn, target_id = await self._process.new_page(self._http)
        try:
            (width, height), preset_dpr, preset_mobile = viewport_for(opts.device)
            await conn.send("Page.enable")
            await conn.send("Runtime.enable")
            await conn.send("Emulation.setDeviceMetricsOverride",
                            width=width, height=height,
                            deviceScaleFactor=preset_dpr * opts.scale,
                            mobile=preset_mobile)
            await conn.send("Page.setDocumentContent",
                            frameId=await self._main_frame(conn), html=html)
            await asyncio.sleep(max(opts.wait_ms, 120) / 1000)
            return await self._capture_by_mode(conn, opts)
        finally:
            await self._process.close_page(self._http, target_id, conn)

    async def _main_frame(self, conn: CDPConnection) -> str:
        tree = await conn.send("Page.getFrameTree")
        return tree["frameTree"]["frame"]["id"]


def _js_string(value: str) -> str:
    """把 Python 字符串安全地嵌进 JS 字面量。"""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f"'{escaped}'"
