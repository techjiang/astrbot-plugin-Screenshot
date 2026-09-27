"""把 ShotOptions 落到浏览器上：导航、等渲染、按模式截图。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import aiohttp

from .browser import (
    MAX_CONCURRENT_PAGES,
    BrowserProcess,
    CDPConnection,
    CDPError,
    decode_frame,
)
from .config import ShotOptions, viewport_for
from .image import normalise_scale

logger = logging.getLogger("astrbot.screenshot")

MAX_CAPTURE_HEIGHT = 20000  # 超过这个高度的整页图会被裁掉，防止 CDP 返回超大帧
NETWORK_IDLE_MS = 800
MAX_HIDE_SELECTORS = 50  # 防止超长指令串把 JS 拼爆


class ScreenshotError(RuntimeError):
    """截图过程中的可预期失败。"""


def _short(text: str, limit: int = 120) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ScreenshotSession:
    """常驻一个 Chromium 进程，每次截图开一个标签页，用完即关。"""

    def __init__(
        self,
        binary: str,
        flags: list[str] | None = None,
        *,
        proxy: str = "",
        headers: dict[str, str] | None = None,
        max_concurrent: int = MAX_CONCURRENT_PAGES,
    ) -> None:
        self.binary = binary
        self.flags = flags
        self.proxy = proxy
        self.headers = dict(headers or {})
        self._process: BrowserProcess | None = None
        self._http: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._pages = asyncio.Semaphore(max(1, min(int(max_concurrent), 16)))

    # ---------- 生命周期 ----------

    async def ensure_started(self) -> None:
        async with self._lock:
            if self._process and self._process.alive:
                return
            if self._process:
                logger.warning("Chromium 进程已退出，重新拉起")
                await self._process.stop()
            process = BrowserProcess(self.binary, self.flags, proxy=self.proxy)
            await process.start()
            if self._http is None or self._http.closed:
                self._http = aiohttp.ClientSession()
            self._process = process
            logger.info(
                "Chromium 已就绪（端口 %s，版本 %s）",
                process.port,
                _short(process.user_agent, 60) or "未知",
            )

    async def shutdown(self) -> None:
        async with self._lock:
            if self._process:
                await self._process.stop()
                self._process = None
            if self._http and not self._http.closed:
                await self._http.close()
            self._http = None

    async def _new_page(self) -> tuple[CDPConnection, str]:
        await self.ensure_started()
        assert self._process is not None and self._http is not None
        try:
            return await self._process.new_page(self._http)
        except CDPError:
            # 进程假死（僵尸标签页占满）时重启一次再试
            logger.warning("新建标签页失败，重启 Chromium 后重试")
            await self._restart()
            assert self._process is not None and self._http is not None
            return await self._process.new_page(self._http)

    async def _restart(self) -> None:
        async with self._lock:
            if self._process:
                await self._process.stop()
                self._process = None
        await self.ensure_started()

    # ---------- 入口 ----------

    async def capture(self, opts: ShotOptions, attempts: int = 2) -> bytes:
        """截一张图；浏览器中途死掉时自动重启并重试一次。"""
        return await self._with_page(
            lambda conn: self._render(conn, opts), label="截图", attempts=attempts
        )

    async def render_html(
        self, html: str, opts: ShotOptions, attempts: int = 2
    ) -> bytes:
        """把一段 HTML 直接渲染成图，不经过网络。"""
        return await self._with_page(
            lambda conn: self._render_html(conn, html, opts),
            label="渲染",
            attempts=attempts,
        )

    async def _with_page(self, action, *, label: str, attempts: int = 2):
        """统一处理「开页 → 干活 → 关页」，失败时重启浏览器重试。"""
        last_error: Exception | None = None
        for attempt in range(max(1, attempts)):
            try:
                async with self._pages:
                    conn, target_id = await self._new_page()
                    try:
                        return await action(conn)
                    finally:
                        await self._close_page(target_id, conn)
            except CDPError as exc:
                last_error = exc
                logger.warning("%s失败（%s），重启浏览器后重试", label, exc)
                await self._restart()
                await asyncio.sleep(0.3)
            except ScreenshotError:
                raise
            except aiohttp.ClientError as exc:
                last_error = exc
                logger.warning("%s时网络异常（%s），重启浏览器后重试", label, exc)
                await self._restart()
                await asyncio.sleep(0.3)
        raise ScreenshotError(f"{label}失败：{last_error}")

    async def _close_page(self, target_id: str, conn: CDPConnection) -> None:
        if self._process and self._http:
            with contextlib.suppress(Exception):
                await self._process.close_page(self._http, target_id, conn)

    # ---------- 渲染 ----------

    async def _prepare(self, conn: CDPConnection, opts: ShotOptions) -> None:
        await conn.send("Page.enable")
        await conn.send("Runtime.enable")
        if self.headers:
            await conn.send(
                "Network.setExtraHTTPHeaders",
                headers={str(k): str(v) for k, v in self.headers.items()},
            )

    async def _apply_metrics(self, conn: CDPConnection, opts: ShotOptions) -> None:
        (width, height), preset_dpr, preset_mobile = viewport_for(opts.device)
        dpr = normalise_scale(preset_dpr * opts.scale)
        mobile = preset_mobile if opts.mobile is None else opts.mobile
        await conn.send(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=dpr,
            mobile=mobile,
            screenWidth=width,
            screenHeight=height,
        )
        await conn.send(
            "Emulation.setEmulatedMedia",
            features=[
                {"name": "prefers-color-scheme", "value": "dark" if opts.dark else "light"}
            ],
        )

    async def _render(self, conn: CDPConnection, opts: ShotOptions) -> bytes:
        timeout = max(opts.timeout_ms, 1000) / 1000

        await self._prepare(conn, opts)
        await self._apply_metrics(conn, opts)
        await self._navigate(conn, opts, timeout)

        if opts.wait_for:
            await self._wait_for_selector(conn, opts.wait_for, timeout)
        if opts.wait_ms:
            await asyncio.sleep(min(opts.wait_ms, 30000) / 1000)
        await self._wait_images(conn, timeout)
        if opts.hide:
            await self._apply_hide(conn, opts.hide)
        if opts.mode == "full":
            await self._pin_fixed_layers(conn)
        if opts.watermark:
            await self._apply_watermark(conn, opts.watermark)

        return await self._capture_with_retry(conn, opts)

    async def _render_html(self, conn: CDPConnection, html: str, opts: ShotOptions) -> bytes:
        await self._prepare(conn, opts)
        await self._apply_metrics(conn, opts)
        await conn.send("Page.setDocumentContent", frameId=await self._main_frame(conn),
                        html=html)
        # setDocumentContent 不触发 load 事件，用 readyState 轮询代替
        deadline = asyncio.get_running_loop().time() + max(opts.timeout_ms, 1000) / 1000
        while asyncio.get_running_loop().time() < deadline:
            state = await self._evaluate(conn, "document.readyState", by_value=True)
            if state == "complete":
                break
            await asyncio.sleep(0.1)
        await self._wait_images(conn, 2.0)
        await asyncio.sleep(max(opts.wait_ms, 120) / 1000)
        if opts.hide:
            await self._apply_hide(conn, opts.hide)
        if opts.mode == "full":
            await self._pin_fixed_layers(conn)
        if opts.watermark:
            await self._apply_watermark(conn, opts.watermark)
        return await self._capture_with_retry(conn, opts)

    async def _navigate(self, conn: CDPConnection, opts: ShotOptions, timeout: float) -> None:
        loaded = conn.subscribe("Page.loadEventFired")
        network = conn.subscribe("Network.loadingFinished")
        try:
            await conn.send("Network.enable")
            result = await conn.send("Page.navigate", timeout=min(timeout + 5, 60), url=opts.url)
            if result.get("errorText"):
                raise ScreenshotError(f"无法打开页面：{result['errorText']}")

            # 先等 load 事件，拿不到就退化成「等到没有新请求」
            try:
                await asyncio.wait_for(loaded.get(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.debug("等待 load 事件超时，继续按网络空闲判断")

            await self._drain_network(network, timeout)
        except CDPError as exc:
            if "超时" in str(exc) or "timeout" in str(exc).lower():
                raise ScreenshotError(f"页面加载超时（>{int(timeout)}s）：{_short(opts.url)}") from exc
            raise ScreenshotError(f"页面加载失败：{exc}") from exc
        finally:
            conn.unsubscribe("Page.loadEventFired", loaded)
            conn.unsubscribe("Network.loadingFinished", network)

        await conn.send(
            "Runtime.evaluate",
            expression=(
                "(document.readyState === 'complete' ? Promise.resolve(1) "
                ": new Promise(r => window.addEventListener('load', () => r(1), {once:true})))"
                ".then(() => (document.fonts && document.fonts.ready) || 1)"
            ),
            awaitPromise=True,
            timeout=min(timeout + 5, 60),
        )

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

    async def _evaluate(self, conn: CDPConnection, expression: str, *,
                        by_value: bool = False, await_promise: bool = False):
        result = await conn.send(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=by_value,
            awaitPromise=await_promise,
        )
        payload = result.get("result", {})
        return payload.get("value") if by_value else payload

    async def _wait_images(self, conn: CDPConnection, timeout: float) -> None:
        """等页面里的图片解码完，避免截到半张图或懒加载占位。"""
        expression = (
            "Promise.all(Array.from(document.images).map(img => "
            "(img.complete && img.naturalWidth > 0) ? 1 : "
            "new Promise(r => { img.addEventListener('load', () => r(1), {once:true});"
            " img.addEventListener('error', () => r(1), {once:true});"
            " setTimeout(() => r(1), 2000); }))).then(() => 1)"
        )
        with contextlib.suppress(CDPError):
            await conn.send(
                "Runtime.evaluate",
                expression=expression,
                awaitPromise=True,
                timeout=max(1.0, min(float(timeout), 10.0)),
            )

    async def _wait_for_selector(self, conn: CDPConnection, selector: str, timeout: float) -> None:
        expression = (
            "(() => { try { return !!document.querySelector(%s) } "
            "catch (e) { return 'invalid' } })()"
        ) % _js_string(selector)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            value = await self._evaluate(conn, expression, by_value=True)
            if value == "invalid":
                raise ScreenshotError(f"选择器不合法：{_short(selector, 60)}")
            if value is True:
                return
            await asyncio.sleep(0.15)
        raise ScreenshotError(f"等待元素超时：{_short(selector, 60)}")

    async def _apply_hide(self, conn: CDPConnection, selectors: list[str]) -> None:
        """隐藏干扰元素；选择器写错只跳过，不影响整张截图。"""
        for selector in selectors[:MAX_HIDE_SELECTORS]:
            await conn.send("Runtime.evaluate", expression=(
                "(() => { try { document.querySelectorAll(%s).forEach("
                "el => el.style.setProperty('display','none','important')) } "
                "catch (e) {} })()"
            ) % _js_string(selector))

    async def _pin_fixed_layers(self, conn: CDPConnection) -> None:
        """整页截图前把固定定位元素钉回页首。

        踩过的坑：``position:fixed`` 元素在 ``captureBeyondViewport`` 的整页长图里
        会被 Chrome 渲染到画面底部，于是导航栏在长图末尾又出现一次。
        这里把它们的 ``top`` 改成绝对偏移并解除 ``bottom`` 约束，视觉上仍停在页首。
        """
        with contextlib.suppress(CDPError):
            await conn.send(
                "Runtime.evaluate",
                expression=(
                    "(() => {"
                    "  const scrollY = window.scrollY || document.documentElement.scrollTop || 0;"
                    "  for (const el of document.querySelectorAll('body *')) {"
                    "    const style = getComputedStyle(el);"
                    "    if (style.position !== 'fixed' || style.display === 'none') continue;"
                    "    if (el.id === '__astrbot_shot_mark__') continue;"
                    "    const rect = el.getBoundingClientRect();"
                    "    if (!rect.height && !rect.width) continue;"
                    "    el.style.setProperty('position', 'absolute', 'important');"
                    "    el.style.setProperty('top', (rect.top + scrollY) + 'px', 'important');"
                    "    el.style.setProperty('bottom', 'auto', 'important');"
                    "    el.style.setProperty('left', rect.left + 'px', 'important');"
                    "    el.style.setProperty('right', 'auto', 'important');"
                    "    el.style.setProperty('width', rect.width + 'px', 'important');"
                    "  }"
                    "})()"
                ),
                timeout=10,
            )

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

    # ---------- 截图 ----------

    async def _capture_with_retry(
        self, conn: CDPConnection, opts: ShotOptions, attempts: int = 3
    ) -> bytes:
        """在遮罩/动画未停止时截到空白图的情况下自动重试。"""
        last: bytes | None = None
        delay = 0.5
        for index in range(max(1, attempts)):
            try:
                data = await self._capture_by_mode(conn, opts)
            except CDPError:
                # 连接层面的错误交给上层重启浏览器，不做原地重试
                raise
            last = data
            if not _is_blank(data):
                return data
            if index < attempts - 1:
                logger.debug("截到空白图，%ss 后重试", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2.0)
        return last or b""

    async def _capture_by_mode(self, conn: CDPConnection, opts: ShotOptions) -> bytes:
        if opts.mode == "element":
            return await self._capture_element(conn, opts)

        full_page = opts.mode == "full"
        clip = None
        if full_page:
            metrics = await conn.send("Page.getLayoutMetrics")
            css = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            height = int(css.get("height", 0) or 0)
            width = int(css.get("width", 0) or 0)
            if height > MAX_CAPTURE_HEIGHT:
                logger.info("整页高度 %spx 超过上限，已裁到 %spx", height, MAX_CAPTURE_HEIGHT)
                height = MAX_CAPTURE_HEIGHT
            if height and width:
                clip = {"x": 0, "y": 0, "width": width, "height": height, "scale": 1}
            else:
                full_page = False

        kwargs = {
            "format": "png",
            "captureBeyondViewport": bool(full_page),
            "fromSurface": True,
            "optimizeForSpeed": False,
        }
        if clip:
            kwargs["clip"] = clip
        result = await conn.send("Page.captureScreenshot", timeout=60, **kwargs)
        return decode_frame(result)

    async def _capture_element(self, conn: CDPConnection, opts: ShotOptions) -> bytes:
        expression = (
            "(() => { try { return document.querySelector(%s) } "
            "catch (e) { return null } })()"
        ) % _js_string(opts.selector)
        payload = await self._evaluate(conn, expression)
        object_id = payload.get("objectId")
        if not object_id:
            raise ScreenshotError(f"页面中没有找到元素：{_short(opts.selector, 60)}")

        try:
            try:
                await conn.send("DOM.enable")
                box = await conn.send("DOM.getBoxModel", timeout=10, objectId=object_id)
            except CDPError as exc:
                raise ScreenshotError(
                    f"元素不可见或不可截取：{_short(opts.selector, 60)}"
                ) from exc

            quad = box["model"]["border"]
            left, top = quad[0], quad[1]
            right, bottom = quad[4], quad[5]
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0:
                raise ScreenshotError(f"元素尺寸为 0，无法截图：{_short(opts.selector, 60)}")
            if height > MAX_CAPTURE_HEIGHT:
                height = MAX_CAPTURE_HEIGHT

            result = await conn.send(
                "Page.captureScreenshot",
                timeout=60,
                format="png",
                captureBeyondViewport=True,
                fromSurface=True,
                clip={"x": left, "y": top, "width": width, "height": height, "scale": 1},
            )
            return decode_frame(result)
        finally:
            with contextlib.suppress(CDPError):
                await conn.send("Runtime.releaseObject", timeout=5, objectId=object_id)

    async def _main_frame(self, conn: CDPConnection) -> str:
        tree = await conn.send("Page.getFrameTree")
        return tree["frameTree"]["frame"]["id"]


def _is_blank(png: bytes, variance_limit: float = 12.0) -> bool:
    """粗判「整张同色」的空白图，用于触发重试。

    灰度降采样后看方差：纯色页面方差接近 0，正常页面方差远大于阈值。
    只做一次 32x32 的统计，几十毫秒级别，不引入额外依赖。
    """
    if len(png) > 2 * 1024 * 1024:  # 大图基本不可能是纯色空白
        return False
    try:
        import io

        from PIL import Image, ImageStat
    except ImportError:
        return False
    try:
        with Image.open(io.BytesIO(png)) as image:
            sample = image.convert("L").resize((32, 32))
            stat = ImageStat.Stat(sample)
        return (stat.var[0] or 0.0) < variance_limit
    except Exception:  # 解析失败就不当作空白，交给上层处理
        return False


def _js_string(value: str) -> str:
    """把 Python 字符串安全地嵌进 JS 字面量。"""
    return json.dumps(str(value), ensure_ascii=False)
