"""把 ShotOptions 落到浏览器上：导航、等渲染、按模式截图。"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging

import aiohttp
from PIL import Image

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

MAX_CAPTURE_HEIGHT = 32000  # 单张位图的高度上限（按 DPR 折算成 CSS 后才能用），
                            # 超过就分段截取再拼接
NETWORK_QUIET_MS = 350  # 请求归零后还需安静这么久，才认为网络空闲
NETWORK_IDLE_CAP_S = 6.0  # 「不再有新请求」的总上限，兜住无限重连的页面
NAVIGATE_TIMEOUT_S = 60.0  # Page.navigate 自身的超时，与「等页面稳定」的预算分开
MAX_HIDE_SELECTORS = 50  # 防止超长指令串把 JS 拼爆


class ScreenshotError(RuntimeError):
    """截图过程中的可预期失败。"""


def _short(text: str, limit: int = 120) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _NetworkMonitor:
    """跟踪页面在途请求，判断「网络是否已经闲下来」。

    老实现是「连续 N 毫秒收不到 loadingFinished 就算空闲」，而 finished 只在
    **有请求完成**时才产生：页面已经全部加载完、一个请求都不发的时候，事件永远
    不会来，于是每次截图都要白白等满一个 N 毫秒的窗口。实测本机页面固定等 800ms
    时单次截图 0.83s，而真实加载只用了 0.03s。

    这里改成计数语义：``requestWillBeSent`` 就 +1，``loadingFinished`` /
    ``loadingFailed`` 就 -1。但只盯 ``inflight`` 还不够——现代前端常挂着长轮询、
    SSE、埋点心跳这类「永不结束」的请求，inflight 永远归不了零。实测截 cnb.cool
    时 inflight 卡在 1，6 秒的静默上限被整段耗光（截图总共 9.4s，其中 6s 是白等）。

    所以真正的判据是「**没有新的请求发出来**」：每收到一个 requestWillBeSent 就
    刷新活动时间，安静 :data:`NETWORK_QUIET_MS` 之后放行，跟 inflight 是否为 0 无关。
    """

    _EVENTS = (
        ("Network.requestWillBeSent", "request"),
        ("Network.loadingFinished", "finish"),
        ("Network.loadingFailed", "finish"),
    )

    def __init__(self, conn: CDPConnection) -> None:
        self._conn = conn
        self._queues: list[tuple[asyncio.Queue, str]] = []
        self._inflight = 0
        self._last_request = 0.0
        self._last_activity = 0.0
        for event, kind in self._EVENTS:
            self._queues.append((conn.subscribe(event), kind))

    def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        for queue, kind in self._queues:
            while not queue.empty():
                queue.get_nowait()
                self._last_activity = loop.time()
                if kind == "request":
                    self._inflight += 1
                    self._last_request = self._last_activity
                else:
                    self._inflight = max(0, self._inflight - 1)

    async def wait_quiet(self, budget: float) -> None:
        """等页面不再发新请求；``budget`` 秒后无条件放行。"""
        quiet = NETWORK_QUIET_MS / 1000
        loop = asyncio.get_running_loop()
        self._last_request = self._last_activity = loop.time()
        deadline = self._last_request + max(0.0, budget)
        while True:
            self._drain()
            now = loop.time()
            # 判据是「最近没有新请求」，不断流的请求（长轮询/SSE）不会把这里拖住
            if self._inflight == 0 and now - self._last_request >= quiet:
                return
            if self._inflight > 0 and now - self._last_activity >= quiet:
                return
            if now >= deadline:
                return
            await asyncio.sleep(min(0.05, max(0.005, deadline - now)))

    def close(self) -> None:
        for (event, _kind), (queue, _unused) in zip(self._EVENTS, self._queues):
            self._conn.unsubscribe(event, queue)


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
        max_dpr: float = 0.0,
        render_watch_ms: int = 0,
    ) -> None:
        self.binary = binary
        self.flags = flags
        self.proxy = proxy
        self.headers = dict(headers or {})
        # 大图自动降 DPR 的上限（0 = 关闭）。
        # MAX_CAPTURE_HEIGHT 是「位图高度」上限，dpr=4 的整页长图很容易撞上去，
        # 于是一张 40000px 的图文页要拆成 9~12 段、逐段截图再拼接（实测 45s）。
        # 这里给「本来就要分段」的整页图降一档 DPR，少截几段、少拼几次。
        self.max_dpr = max(0.0, float(max_dpr or 0.0))
        # 截图前观察 DOM 变化的窗口（毫秒）：0 表示用内置默认值
        self.render_watch_ms = max(0, int(render_watch_ms or 0))
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

    @staticmethod
    async def _apply_transparency(conn: CDPConnection, opts: ShotOptions) -> None:
        """把默认背景刷成全透明，让 PNG 真正带上 alpha 通道。

        踩过的坑：``Page.captureScreenshot`` 默认把「白底」烘焙进图里 —— 实测一个
        ``body{background:transparent}`` 的页面，出图是 ``RGB`` 模式、空白处像素为
        ``(255,255,255,255)``，无背景的 Logo/图标截出来全是白块。

        ``omitBackground=True`` 单独传也没用；必须先用
        ``Emulation.setDefaultBackgroundColorOverride`` 把默认背景设成 ``alpha=0``，
        两层配合才会返回 ``RGBA``。
        """
        if opts.transparent:
            await conn.send(
                "Emulation.setDefaultBackgroundColorOverride",
                color={"r": 0, "g": 0, "b": 0, "a": 0},
            )
        else:
            with contextlib.suppress(CDPError):
                await conn.send("Emulation.setDefaultBackgroundColorOverride")

    async def _apply_metrics(self, conn: CDPConnection, opts: ShotOptions) -> None:
        await conn.send(
            "Emulation.setDeviceMetricsOverride",
            **self._metrics_params(opts),
        )
        await conn.send("Emulation.setEmulatedMedia", **self._media_params(opts))
        # setDocumentContent / 视口变更都会重置默认背景，这里每次都补齐
        await self._apply_transparency(conn, opts)

    @staticmethod
    def _metrics_params(opts: ShotOptions) -> dict:
        """``Emulation.setDeviceMetricsOverride`` 的完整参数。

        单独抽出来是为了**补发**：``Page.setDocumentContent`` 会重建渲染器并
        丢掉 device metrics，所以那一处需要原样重设一次。
        """
        (width, height), preset_dpr, preset_mobile = viewport_for(opts.device)
        dpr = normalise_scale(preset_dpr * opts.scale)
        mobile = preset_mobile if opts.mobile is None else opts.mobile
        return {
            "width": width,
            "height": height,
            "deviceScaleFactor": dpr,
            "mobile": mobile,
            "screenWidth": width,
            "screenHeight": height,
        }

    @staticmethod
    def _media_params(opts: ShotOptions) -> dict:
        """媒体模拟参数：配色方案 + 可选的打印媒体。

        ``print_media`` 会把页面切到 ``screen`` → ``print`` 的媒体查询分支，
        拿到网页自己的打印样式（有的站点只在 ``@media print`` 里去掉导航与浮层），
        配合已有能力可以出「干净的正文图」。
        """
        params: dict = {
            "features": [
                {"name": "prefers-color-scheme", "value": "dark" if opts.dark else "light"}
            ]
        }
        if opts.print_media:
            params["media"] = "print"
        return params

    async def _render(self, conn: CDPConnection, opts: ShotOptions) -> bytes:
        timeout = max(opts.timeout_ms, 1000) / 1000

        await self._prepare(conn, opts)
        await self._apply_metrics(conn, opts)
        await self._navigate(conn, opts, timeout)

        if opts.wait_for:
            await self._wait_for_selector(conn, opts.wait_for, timeout)
        if opts.wait_ms:
            await asyncio.sleep(min(opts.wait_ms, 30000) / 1000)
        # 观察窗口放在 waitms 之后：用户已经明确等过了，这里再兜一层「刚变化完」
        await self._wait_images(conn, timeout)
        if opts.hide:
            await self._apply_hide(conn, opts.hide)
        full_page = opts.mode == "full"
        visible_height = None
        if full_page:
            # 整页超限被分段时，水印要贴到「实际画面」的底部
            visible_height = await self._full_page_height(conn, opts)
        rect = await self._element_rect(conn, opts) if opts.mode == "element" else None
        if opts.watermark:
            await self._apply_watermark(
                conn, opts.watermark, full_page=full_page,
                visible_height=visible_height, element=rect,
            )

        return await self._capture_with_retry(
            conn, opts, full_page=full_page, visible_height=visible_height, element=rect
        )

    async def _render_html(self, conn: CDPConnection, html: str, opts: ShotOptions) -> bytes:
        await self._prepare(conn, opts)
        await self._apply_metrics(conn, opts)
        await conn.send("Page.setDocumentContent", frameId=await self._main_frame(conn),
                        html=html)
        # 踩过的坑：Page.setDocumentContent 会重建渲染器，丢掉 device metrics，
        # 页面便回落到 980px 宽的移动端默认视口，且浏览器会在画面顶部画出原生标题栏
        # （中文标题渲染成「????」豆腐块）。这里重发一次 metrics，把视口钉回来。
        await conn.send("Emulation.setDeviceMetricsOverride", **self._metrics_params(opts))
        await conn.send("Emulation.setEmulatedMedia", **self._media_params(opts))
        await self._apply_transparency(conn, opts)
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
        full_page = opts.mode == "full"
        visible_height = None
        if full_page:
            visible_height = await self._full_page_height(conn, opts)
        rect = await self._element_rect(conn, opts) if opts.mode == "element" else None
        if opts.watermark:
            await self._apply_watermark(
                conn, opts.watermark, full_page=full_page,
                visible_height=visible_height, element=rect,
            )
        return await self._capture_with_retry(
            conn, opts, full_page=full_page, visible_height=visible_height, element=rect
        )

    async def _navigate(self, conn: CDPConnection, opts: ShotOptions, timeout: float) -> None:
        loaded = conn.subscribe("Page.loadEventFired")
        monitor = _NetworkMonitor(conn)
        try:
            await conn.send("Network.enable")
            # Page.navigate 的 timeout 是「导航阶段」自身的超时，不该拿调用方的
            # 整页预算去顶：opts.timeout_ms 只负责「等页面稳定」那一段。
            if not opts.url.strip():
                raise ScreenshotError("缺少要打开的网址，例如 /截图 example.com")
            result = await conn.send("Page.navigate", timeout=NAVIGATE_TIMEOUT_S, url=opts.url)
            if result.get("errorText"):
                raise ScreenshotError(f"无法打开页面：{result['errorText']}")

            # 先等 load 事件，拿不到就退化成「等到没有新请求」
            try:
                await asyncio.wait_for(loaded.get(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.debug("等待 load 事件超时，继续按网络空闲判断")

            await monitor.wait_quiet(min(NETWORK_IDLE_CAP_S, timeout))
        except CDPError as exc:
            if "超时" in str(exc) or "timeout" in str(exc).lower():
                raise ScreenshotError(f"页面加载超时（>{int(timeout)}s）：{_short(opts.url)}") from exc
            raise ScreenshotError(f"页面加载失败：{exc}") from exc
        finally:
            conn.unsubscribe("Page.loadEventFired", loaded)
            monitor.close()

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
        """等图片解码完，并给「截图前才挂上去的图」留一个观察窗口。

        踩过的坑（实测复现）：页面上常见 ``setTimeout(() => img.src = ...)`` 这类
        延迟挂图。老实现只统计**调用那一刻**已在 DOM 里的图片 —— 实测一个 700ms 后
        才插图的区块，元素截图拿到的是灰色占位「图片加载中…」，而同样的用例加上
        ``waitms=1500`` 就正常。也就是说默认链路对「DOM 稍后才变化」完全没有容错。

        这里改成两段式：

        1. 观察窗口内用 MutationObserver 盯 ``<img>`` 的新增/``src`` 变更，
           有新图进来就继续等它解码；
        2. 最后再统一等所有未解码完的图片（有上限，不会被永不结束的图拖住）。

        观察窗口默认 250ms（可用 ``render_watch_ms`` 调整）—— 比盲目
        ``waitms=1500`` 便宜，又能兜住绝大多数「导航完成后一拍才插图」的写法。
        """
        default_watch = max(250, min(int(NETWORK_QUIET_MS), 800))
        watch_ms = self.render_watch_ms or default_watch
        expression = (
            "new Promise(resolve => {"
            "  const pending = () => Array.from(document.images).filter("
            "    img => !(img.complete && img.naturalWidth > 0));"
            "  let timer = null;"
            f"  const done = () => {{ if (timer) clearTimeout(timer);"
            "    Promise.race(["
            "      Promise.all(pending().map(img => new Promise(r => {"
            "        img.addEventListener('load', () => r(1), {once:true});"
            "        img.addEventListener('error', () => r(1), {once:true});"
            "      }))),"
            "      new Promise(r => setTimeout(() => r(1), 1200))"
            "    ]).then(() => resolve(1)); };"
            "  const observer = new MutationObserver(() => {"
            f"    if (timer) clearTimeout(timer); timer = setTimeout(done, {watch_ms});"
            "  });"
            "  observer.observe(document.documentElement, {"
            "    childList: true, subtree: true, attributes: true,"
            "    attributeFilter: ['src', 'srcset', 'style', 'class'] });"
            f"  timer = setTimeout(() => {{ observer.disconnect(); done(); }}, {watch_ms});"
            "})"
        )
        with contextlib.suppress(CDPError):
            await conn.send(
                "Runtime.evaluate",
                expression=expression,
                awaitPromise=True,
                timeout=max(2.0, min(float(timeout), 12.0)),
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

    async def _expand_viewport_to(self, conn: CDPConnection, opts: ShotOptions,
                                  height: int, dpr: float | None = None) -> None:
        """整页截图前把运行时视口拉高到目标高度。

        踩过的坑（这次实测才定位到根因）：``position:fixed`` 元素是相对**布局视口**
        定位的，而 ``captureBeyondViewport`` 只扩大「截取范围」，布局视口仍是
        ``Emulation`` 里设的那点高度。于是文档底部右下角的固定浮层会落在
        ``viewport_height`` 处（800x600 视口 + 6000px 文档 → 浮层出现在 y=552），
        在整页长图里就是「浮层跑到画面中部」。

        曾经的做法是把 fixed 元素改成 ``absolute`` + 写死 ``top``/``width``。
        实测不但没能移动像素（改完浮层仍在 y=552），还会把 ``width`` 写死成拍下来的
        数值，滚动条一出现就切掉 10 多 px。同一手法也会把 ``sticky`` 一起破坏。

        正解是把视口高度临时拉到目标高度：视口底 == 文档底，fixed 元素自然落在
        正确位置，``sticky`` 也照常工作。
        """
        (width, _height), preset_dpr, preset_mobile = viewport_for(opts.device)
        dpr = self._requested_dpr(opts) if dpr is None else dpr
        mobile = preset_mobile if opts.mobile is None else opts.mobile
        await conn.send(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=max(1, int(height)),
            deviceScaleFactor=dpr,
            mobile=mobile,
            screenWidth=width,
            screenHeight=max(1, int(height)),
        )
        # 让渲染器按新视口重排一帧，否则裁出来的还是旧布局
        await self._next_frame(conn)

    async def _next_frame(self, conn: CDPConnection) -> None:
        with contextlib.suppress(CDPError):
            await conn.send(
                "Runtime.evaluate",
                expression=(
                    "new Promise(r => requestAnimationFrame("
                    "() => requestAnimationFrame(() => r(1))))"
                ),
                awaitPromise=True,
                timeout=5,
            )

    async def _apply_watermark(
        self, conn: CDPConnection, text: str, *, full_page: bool,
        visible_height: int | None = None, element: dict | None = None,
    ) -> None:
        """打右下角水印。

        踩过的坑：水印用 ``position:fixed`` 只贴**视口**底，整页长图里它就跑到画面
        正中盖住正文。改 ``absolute`` 也不行——页面没有定位祖先时 ``absolute`` 依然
        相对初始包含块解析，``top`` 设成文档高度会把水印直接挤出画面。

        整页模式的实际画面底是 ``min(文档高, 可截取高度)``，所以：
        把水印挂到 ``<html>`` 上用绝对定位，``top`` 取这个值再上移一个水印高度。

        元素模式又是另一种情况：裁剪框只覆盖元素自己那块，水印若还按「文档底」定位，
        就会落到框外 —— 实测元素图里水印**整块消失**（140x37 的图里灰色胶囊占比 0）。
        所以元素模式把水印贴着 ``element`` 的右下角放。
        """
        label = _js_string(text)
        if element:
            # 贴元素右下角：偏移量按元素尺寸夹取，元素很小时也不会把水印挤出裁剪框
            element_left = element["left"]
            element_top = element["top"]
            element_right = element["right"]
            element_bottom = element["bottom"]
            offset_x = min(92.0, max(4.0, element["width"] * 0.06))
            offset_y = 30.0
            geometry = (
                "box.style.position = 'absolute';"
                f"box.style.left = Math.max({element_left}, "
                f"{element_right} - {offset_x:.1f}) + 'px';"
                f"box.style.top = Math.max({element_top}, "
                f"{element_bottom} - {offset_y:.1f}) + 'px';"
                "box.style.right = 'auto';"
                "box.style.bottom = 'auto';"
            )
        elif full_page:
            limit = (
                "Math.min(documentElement.scrollHeight, %d)" % int(visible_height)
                if visible_height
                else "documentElement.scrollHeight"
            )
            geometry = (
                "box.style.position = 'absolute';"
                f"box.style.top = Math.max(0, {limit} - 34) + 'px';"
                "box.style.bottom = 'auto';"
            )
        else:
            geometry = (
                "box.style.position = 'fixed';"
                "box.style.bottom = '10px';"
                "box.style.top = 'auto';"
            )
        script = (
            "(() => {"
            "  const documentElement = document.documentElement;"
            "  const id = '__astrbot_shot_mark__';"
            "  const old = document.getElementById(id);"
            "  if (old) old.remove();"
            "  const box = document.createElement('div');"
            "  box.id = id;"
            f"  box.textContent = {label};"
            "  box.style.right = '12px';"
            "  box.style.zIndex = '2147483647';"
            "  box.style.font = '12px/1.6 sans-serif';"
            "  box.style.color = '#fff';"
            "  box.style.background = 'rgba(0,0,0,.45)';"
            "  box.style.padding = '2px 10px';"
            "  box.style.borderRadius = '10px';"
            "  box.style.pointerEvents = 'none';"
            f"  {geometry}"
            "  documentElement.appendChild(box);"
            "})()"
        )
        await conn.send("Runtime.evaluate", expression=script)

    # ---------- 截图 ----------

    async def _capture_with_retry(
        self, conn: CDPConnection, opts: ShotOptions, attempts: int = 3, *,
        full_page: bool = False, visible_height: int | None = None,
        element: dict | None = None,
    ) -> bytes:
        """在遮罩/动画未停止时截到空白图的情况下自动重试。"""
        last: bytes | None = None
        delay = 0.5
        for index in range(max(1, attempts)):
            try:
                if opts.watermark:
                    # 长图模式下页面高度可能刚稳定，重贴一次让水印始终贴文档底部
                    await self._apply_watermark(
                        conn, opts.watermark, full_page=full_page,
                        visible_height=visible_height, element=element,
                    )
                data = await self._capture_by_mode(conn, opts, element=element)
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

    async def _full_page_height(self, conn: CDPConnection, opts: ShotOptions) -> int:
        """整页截图要覆盖的 CSS 高度。

        ``clip`` 的高度会再乘 ``deviceScaleFactor`` 出图，所以要按 DPR 折算
        :data:`MAX_CAPTURE_HEIGHT`，否则手机预设（dpr=3）下 20000 CSS 会变成
        60000px 的位图、把渲染进程拖垮。注意这里返回的**不是**最终裁剪高度：
        超过单张上限的部分由 :meth:`_capture_full_page` 分段截取后拼接。
        """
        metrics = await conn.send("Page.getLayoutMetrics")
        css = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
        height = int(css.get("height", 0) or 0)
        _, preset_dpr, _ = viewport_for(opts.device)
        dpr = normalise_scale(preset_dpr * opts.scale)
        css_limit = max(1000, int(MAX_CAPTURE_HEIGHT / max(dpr, 0.2)))
        if height > css_limit:
            logger.debug(
                "整页高度 %spx（DPR %.2f）超过单张上限 %spx，将分段截取",
                height, dpr, css_limit,
            )
        return height

    def _requested_dpr(self, opts: ShotOptions) -> float:
        _, preset_dpr, _ = viewport_for(opts.device)
        return normalise_scale(preset_dpr * opts.scale)

    def _effective_dpr(self, opts: ShotOptions, page_height: int) -> float:
        """大图自动降 DPR：整页本来就要分段时，降一档换更少的段数与更短的耗时。"""
        dpr = self._requested_dpr(opts)
        if self.max_dpr <= 0 or dpr <= self.max_dpr:
            return dpr
        if page_height and page_height <= self._budget_for(dpr):
            return dpr  # 一张就能装下，没必要降
        return self.max_dpr

    @staticmethod
    def _budget_for(dpr: float) -> int:
        """给定 DPR 时，单张位图能装下的 CSS 高度。"""
        return max(1000, int(MAX_CAPTURE_HEIGHT / max(dpr, 0.2)))

    def _capture_budget(self, opts: ShotOptions, dpr: float | None = None) -> int:
        """本次请求单张位图的 CSS 高度上限。"""
        return self._budget_for(self._requested_dpr(opts) if dpr is None else dpr)

    async def _capture_by_mode(
        self, conn: CDPConnection, opts: ShotOptions, element: dict | None = None
    ) -> bytes:
        if opts.mode == "element":
            return await self._capture_element(conn, opts, element=element)
        if opts.mode == "full":
            return await self._capture_full_page(conn, opts)

        result = await conn.send(
            "Page.captureScreenshot",
            timeout=60,
            format="png",
            captureBeyondViewport=False,
            fromSurface=True,
            optimizeForSpeed=False,
            **self._alpha_param(opts),
        )
        return self._normalise_metrics_frame(decode_frame(result), opts)

    def _normalise_metrics_frame(self, png: bytes, opts: ShotOptions) -> bytes:
        """按 ``Emulation`` 里设定的 ``deviceScaleFactor`` 把截图缩回 CSS 尺寸。

        单独抽出来是因为这是**实测出来的内核行为**，不是可选的优化：

        ``Page.captureScreenshot`` 在**设置了 device metrics override** 时返回的位图
        尺寸是 ``视口CSS尺寸 × deviceScaleFactor`` —— 实测 ``800x600 / dsf=2`` 出图
        ``1600x1200``，``dsf=1`` 出图 ``800x600``。未设 override 时才是「按窗口 DPR
        缩放」，实测默认窗口 ``780x437`` → 出图 ``780x437``。

        于是 ``/截图 example.com viewport 1280x800`` 这种「显式视口」写法会**多出一倍
        像素**（``1280x800`` 出 ``2560x1600``）：不是渲染错，是全链路统一用 DPR=2 描述
        「显式视口」，而内核又按同一个 DPR 放大了一次。位图比预期大 4 倍还意味着
        手机预设下的长图更容易撞到单张上限、被多切几段。

        这里按同一个 DPR 还原，让「输出像素 = 用户指定的视口像素」成立，
        与设备预设（``desktop`` 等自带 DPR 语义）的行为保持一致的直觉。
        非 2 的整数倍缩放一律不动，避免把「DPR 不是整数、或裁剪已带 scale」的情形误缩。
        """
        (width, height), preset_dpr, _mobile = viewport_for(opts.device)
        factor = normalise_scale(preset_dpr * opts.scale)
        if factor <= 1 or abs(factor - round(factor)) > 1e-6:
            return png
        try:
            with Image.open(io.BytesIO(png)) as frame:
                frame.load()
                expected = (max(1, int(round(width * factor))),
                            max(1, int(round(height * factor))))
                if frame.size != expected:
                    return png
                restored = frame.resize((width, height), Image.LANCZOS)
                try:
                    buffer = io.BytesIO()
                    restored.save(buffer, format="PNG", optimize=True)
                    return buffer.getvalue()
                finally:
                    restored.close()
        except (OSError, ValueError):
            logger.debug("视口截图尺寸还原失败，按原始位图返回")
            return png

    async def _document_size(self, conn: CDPConnection,
                             opts: ShotOptions) -> tuple[int, int]:
        """页面的 CSS 尺寸。

        宽度要夹回视口：页面若没写 ``<meta name="viewport" content="width=device-width">``，
        移动端布局会落到 980px 的默认包含块（实测同一个页面：无 meta 时
        ``innerWidth=980`` / 文档高 11264，有 meta 时 ``innerWidth=390`` / 文档高 17368），
        而 ``Emulation`` 里设的 390 只在包含块宽度上生效。此时整页图会比视口宽 2.5 倍、
        每一条分段的版面也全错。夹回视口宽度后，超宽页面被横向裁掉 —— 这与浏览器
        自带「整页截图」的行为一致（本来也不打算横向滚）。真要完整宽度就别开移动端预设。
        """
        (view_width, _view_height), _dpr, mobile = viewport_for(opts.device)
        if opts.mobile is not None:
            mobile = bool(opts.mobile)
        metrics = await conn.send("Page.getLayoutMetrics")
        css = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
        width = int(css.get("width", 0) or 0)
        height = int(css.get("height", 0) or 0)
        if mobile:
            inner = await self._evaluate(conn, "window.innerWidth || 0", by_value=True)
            try:
                inner = int(inner or 0)
            except (TypeError, ValueError):
                inner = 0
            if inner > 0:
                width = min(width or inner, inner)
        return max(1, width or view_width), height

    async def _capture_full_page(self, conn: CDPConnection, opts: ShotOptions) -> bytes:
        """整页截图：视口拉到文档高，超出单张上限时分段截取再纵向拼接。"""
        width, height = await self._document_size(conn, opts)
        if not width or not height:
            # 量不到文档尺寸时退化成可视区截图，别把整次请求废掉
            return await self._capture_by_mode_plain(conn, opts)

        dpr = self._effective_dpr(opts, height)
        if dpr != self._requested_dpr(opts):
            logger.info(
                "页面高 %spx，DPR 由 %.2f 降到 %.2f 以减少分段", height,
                self._requested_dpr(opts), dpr,
            )
        budget = self._capture_budget(opts, dpr)
        if height <= budget:
            return await self._shoot_clip(conn, 0, width, height, opts, dpr)

        logger.debug("整页 %spx 超过单张 %spx，分 %d 段截取",
                     height, budget, -(-height // budget))
        return await self._shoot_strips(conn, width, height, budget, opts, dpr)

    async def _capture_by_mode_plain(self, conn: CDPConnection, opts: ShotOptions) -> bytes:
        result = await conn.send(
            "Page.captureScreenshot",
            timeout=60,
            format="png",
            captureBeyondViewport=False,
            fromSurface=True,
            **self._alpha_param(opts),
        )
        return decode_frame(result)

    async def _shoot_clip(self, conn: CDPConnection, top: int, width: int,
                          height: int, opts: ShotOptions, dpr: float) -> bytes:
        """把视口拉到目标高度后截一张整页。"""
        await self._expand_viewport_to(conn, opts, height, dpr)
        clip = {"x": 0, "y": top, "width": width, "height": height, "scale": 1}
        try:
            result = await conn.send(
                "Page.captureScreenshot",
                timeout=120,
                format="png",
                captureBeyondViewport=True,
                fromSurface=True,
                optimizeForSpeed=False,
                clip=clip,
                **self._alpha_param(opts),
            )
        finally:
            # 视口要还原，否则后续在页面上做的量测与截图都会按被拉高的布局来
            await self._apply_metrics_quiet(conn, opts)
        return decode_frame(result)

    @staticmethod
    def _plan_strips(height: int, budget: int) -> list[tuple[int, int]]:
        """把 ``height`` 切成若干条 ``(top, part)``，避免最后一条只剩几十像素。

        踩过的坑：页面 84016px、budget 6000 时按整数分段，最后一条只有 16px。
        后果不是「多一张图」这么简单 —— 水印贴在文档底 (``height-34``)，恰好落进
        倒数第二条，于是断言/肉眼都会认为「水印跑到画面中部」；聊天里还会多出
        一张 16px 的碎片图。

        策略：条数按上取整定下后，把总高**均分**到这些条上，让每条尽量等长。
        这样最后一条至少有 ``budget/2`` 以上的高度，水印与内容都落在同一条里。
        """
        if height <= budget:
            return [(0, height)]
        count = -(-height // budget)  # 上取整
        part = -(-height // count)    # 均分后每条的高度，同样上取整
        strips: list[tuple[int, int]] = []
        top = 0
        while top < height:
            size = min(part, height - top)
            strips.append((top, size))
            top += size
        return strips

    async def _shoot_strips(self, conn: CDPConnection, width: int, height: int,
                            budget: int, opts: ShotOptions, dpr: float) -> bytes:
        """超长页面分条截取，再拼成一张完整 PNG。

        为什么不直接把整高塞进一个 ``clip``：位图高度会再乘 DPR，dpr=3 时
        20000 CSS 就是 60000px 的位图，渲染进程给不出这么多像素，超出部分会整片
        返回纯色空白。

        每一条都遵循同一个约定 —— **让视口底恰好落在这一条的底部**：
        视口高度设成本条高度，再 ``window.scrollTo(0, top)``。这样
        ``position:fixed`` 元素正好停在分条底边，跟整页一次截到底时落点一致；
        只拉高视口不滚动的话，fixed 元素会跑到**第一条**的底边，
        于是 36000px 的长图里，「回顶部」按钮出现在第 20000px 而不是图底。
        """
        frames: list[Image.Image] = []
        try:
            await conn.send("Emulation.setScrollbarsHidden", hidden=True)
            strips = self._plan_strips(height, budget)
            for index, (top, part) in enumerate(strips):
                await self._expand_viewport_to(conn, opts, part, dpr)
                await conn.send(
                    "Runtime.evaluate",
                    expression=f"window.scrollTo(0, {int(top)})",
                    timeout=5,
                )
                await self._next_frame(conn)
                clip = {"x": 0, "y": top, "width": width, "height": part, "scale": 1}
                result = await conn.send(
                    "Page.captureScreenshot",
                    timeout=120,
                    format="png",
                    captureBeyondViewport=True,
                    fromSurface=True,
                    optimizeForSpeed=False,
                    clip=clip,
                    **self._alpha_param(opts),
                )
                with Image.open(io.BytesIO(decode_frame(result))) as frame:
                    # 透明请求下保留 alpha，否则拼接这一步就把通道丢了
                    frames.append(
                        frame.convert("RGBA") if opts.transparent else frame.convert("RGB")
                    )
                logger.debug("分段截图 %d/%d（y=%s, 高 %s）",
                             index + 1, len(strips), top, part)

            canvas = Image.new(
                "RGBA" if opts.transparent else "RGB",
                (max(frame.width for frame in frames),
                 sum(frame.height for frame in frames)),
                (0, 0, 0, 0) if opts.transparent else (255, 255, 255),
            )
            offset = 0
            for frame in frames:
                canvas.paste(frame, (0, offset))
                offset += frame.height
            buffer = io.BytesIO()
            canvas.save(buffer, format="PNG", optimize=False)
            canvas.close()
            return buffer.getvalue()
        finally:
            for frame in frames:
                frame.close()
            with contextlib.suppress(CDPError):
                await conn.send("Emulation.setScrollbarsHidden", hidden=False)
            await self._apply_metrics_quiet(conn, opts)

    async def _apply_metrics_quiet(self, conn: CDPConnection, opts: ShotOptions) -> None:
        with contextlib.suppress(CDPError):
            await self._apply_metrics(conn, opts)
            await self._next_frame(conn)

    @staticmethod
    def _alpha_param(opts: ShotOptions) -> dict:
        """透明背景请求时给 captureScreenshot 带上 omitBackground。"""
        return {"omitBackground": True} if opts.transparent else {}

    async def _element_rect(self, conn: CDPConnection, opts: ShotOptions) -> dict:
        """算出元素截图的裁剪矩形（视觉盒 + padding），并夹回页面范围内。

        单独抽出来是因为**水印与裁剪必须共用同一个矩形**：水印要贴在元素图的右下角，
        若两边各算一次、或水印仍按「文档底」定位，元素图里就会看不到水印。
        """
        object_id = await self._resolve_element(conn, opts)
        try:
            return await self._measure_element(conn, opts, object_id)
        finally:
            with contextlib.suppress(CDPError):
                await conn.send("Runtime.releaseObject", timeout=5, objectId=object_id)

    async def _resolve_element(self, conn: CDPConnection, opts: ShotOptions) -> str:
        """把选择器解析成 DOM 对象；顺带区分「选择器非法」与「元素不存在」。"""
        expression = (
            "(() => {"
            "  try { document.querySelector(%s); return 'ok' }"
            "  catch (e) { return 'invalid' }"
            "})()"
        ) % _js_string(opts.selector)
        state = await self._evaluate(conn, expression, by_value=True)
        if state == "invalid":
            raise ScreenshotError(f"选择器不合法：{_short(opts.selector, 60)}")

        payload = await self._evaluate(
            conn, "document.querySelector(%s)" % _js_string(opts.selector)
        )
        object_id = payload.get("objectId")
        if not object_id:
            raise ScreenshotError(f"页面中没有找到元素：{_short(opts.selector, 60)}")
        return object_id

    async def _measure_element(
        self, conn: CDPConnection, opts: ShotOptions, object_id: str
    ) -> dict:
        try:
            await conn.send("DOM.enable")
            box = await conn.send("DOM.getBoxModel", timeout=10, objectId=object_id)
        except CDPError as exc:
            # 元素在页面上、但拿不到盒模型：隐藏 / display:none / 被移除
            raise ScreenshotError(
                f"元素不可见或不可截取：{_short(opts.selector, 60)}"
            ) from exc

        quads = await self._element_visual_quads(conn, box, object_id)
        left = min(q[0] for q in quads)
        top = min(q[1] for q in quads)
        right = max(q[2] for q in quads)
        bottom = max(q[3] for q in quads)
        # padding= 让用户给截图留白（阴影/描边/圆角光晕会被 border box 切掉）
        if opts.padding:
            left -= opts.padding
            top -= opts.padding
            right += opts.padding
            bottom += opts.padding

        # 夹回页面范围：clip 的负坐标会被渲染进程当成 0，图会整体错位
        page_w, page_h = await self._document_size(conn, opts)
        left = min(max(0.0, left), float(page_w))
        top = min(max(0.0, top), float(max(page_h, 1)))
        right = min(max(left + 1.0, right), float(page_w))
        bottom = min(max(top + 1.0, bottom), float(max(page_h, top + 1)))

        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            raise ScreenshotError(f"元素尺寸为 0，无法截图：{_short(opts.selector, 60)}")
        # 元素也可能高过单张上限（长列表、整篇文章），夹到上限而不是把
        # 一个 6 万像素的 clip 甩给渲染进程 —— 那样只会拿到半张空白
        budget = self._capture_budget(opts)
        if height > budget:
            logger.info(
                "元素高 %spx 超过单张上限 %spx，已裁到 %spx",
                int(height), budget, budget,
            )
            height = float(budget)
        return {"left": left, "top": top, "right": left + width,
                "bottom": top + height, "width": width, "height": height}

    async def _capture_element(
        self, conn: CDPConnection, opts: ShotOptions, *, element: dict | None = None
    ) -> bytes:
        rect = element or await self._element_rect(conn, opts)
        result = await conn.send(
            "Page.captureScreenshot",
            timeout=120,
            format="png",
            captureBeyondViewport=True,
            fromSurface=True,
            clip={
                "x": rect["left"], "y": rect["top"],
                "width": max(1.0, rect["width"]),
                "height": max(1.0, rect["height"]),
                "scale": 1,
            },
            **self._alpha_param(opts),
        )
        return decode_frame(result)

    async def _element_visual_quads(
        self, conn: CDPConnection, box: dict, object_id: str
    ) -> list[tuple[float, float, float, float]]:
        """元素的「视觉包围盒」，而不是 DOM 的 border box。

        踩过的坑：``DOM.getBoxModel`` 给的是 border box，只取它当裁剪范围时，
        ``box-shadow``、``outline``、``border-radius`` 的圆角光晕全都在框外 ——
        实测一个 300x120、带 40px 红色外阴影 + 6px 绿色 outline 的卡片，截出来
        阴影与描边**整圈消失**，圆角被切成硬边。

        正解是取 ``DOM.getContentQuads`` 的**全部四边形**（元素被折行或被拆成多块时
        会有多个 quad），再并上 border box，取并集作为裁剪范围。
        """
        spans: list[tuple[float, float, float, float]] = []
        border = box.get("model", {}).get("border") or []
        if len(border) >= 8:
            spans.append((border[0], border[1], border[4], border[5]))
        try:
            result = await conn.send(
                "DOM.getContentQuads", timeout=10, objectId=object_id
            )
        except (CDPError, KeyError, TypeError):
            result = {}
        for quad in result.get("quads") or []:
            if len(quad) >= 8:
                xs, ys = quad[0::2], quad[1::2]
                spans.append((min(xs), min(ys), max(xs), max(ys)))
        return spans or [(0.0, 0.0, 0.0, 0.0)]

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
