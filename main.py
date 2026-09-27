"""Screenshot —— 新一代非 API 接口 AstrBot 截图插件。

插件只通过 CDP（Chrome DevTools Protocol）驱动浏览器内核，不引入
Playwright / Selenium / html2image 这类封装库，因此没有浏览器驱动版本漂移问题。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

from .core.browser import find_browser
from .core.config import ShotOptions, parse_instruction
from .core.image import to_bytes, wrap_html
from .core.session import ScreenshotError, ScreenshotSession

logger = logging.getLogger("astrbot.screenshot")

_URL_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


class ScreenshotPlugin(Star):
    """Screenshot · CDP 直驱截图

    /截图 <网址> [view|viewport|full] [元素选择器] [设备] [scale=2] [dark] \
[hide=.a,.b] [watermark=文本] [wait=选择器] [waitms=毫秒]
    /元素截图 <网址> <CSS选择器>
    /渲染截图 <HTML 片段>
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._session: ScreenshotSession | None = None

    # ---------- 生命周期 ----------

    async def _get_session(self) -> ScreenshotSession:
        if self._session is None:
            binary = find_browser(str(self._conf("browser_path", "")))
            self._session = ScreenshotSession(binary, self._conf("launch_flags", []))
        return self._session

    async def terminate(self) -> None:
        if self._session:
            await self._session.shutdown()
            self._session = None

    def _conf(self, key: str, default=None):
        try:
            return self.config.get(key, default)
        except AttributeError:  # 配置缺省时回退默认值，不影响插件启动
            return default

    # ---------- 指令 ----------

    @filter.command("截图")
    async def screenshot(self, event: AstrMessageEvent, instruction: GreedyStr = GreedyStr):
        async for result in self._shoot(event, str(instruction), mode="url"):
            yield result

    @filter.command("元素截图")
    async def element_screenshot(
        self, event: AstrMessageEvent, instruction: GreedyStr = GreedyStr
    ):
        async for result in self._shoot(event, str(instruction), mode="element"):
            yield result

    @filter.command("渲染截图")
    async def render_screenshot(
        self, event: AstrMessageEvent, instruction: GreedyStr = GreedyStr
    ):
        async for result in self._shoot(event, str(instruction), mode="render"):
            yield result

    # ---------- 实现 ----------

    async def _shoot(self, event: AstrMessageEvent, instruction: str, mode: str):
        instruction = instruction.strip()
        if not instruction:
            yield event.plain_result(HELP_TEXT)
            return

        try:
            opts = self._build_options(instruction, mode)
            payloads = await self._capture(opts)
        except ValueError as exc:
            yield event.plain_result(f"参数有误：{exc}")
            return
        except (ScreenshotError, RuntimeError) as exc:
            logger.warning("截图失败：%s", exc)
            yield event.plain_result(f"截图失败：{exc}")
            return

        for path in self._persist(event, payloads):
            yield event.image_result(str(path))

    def _build_options(self, instruction: str, mode: str) -> ShotOptions:
        if mode == "render":
            opts = ShotOptions(mode="render")
            opts.url = instruction[5:] if instruction.lower().startswith("html:") else instruction
            opts.timeout_ms = int(self._conf("timeout_ms", 20000))
            return opts

        opts = parse_instruction(instruction, defaults=self.config)
        if mode == "element":
            opts.mode = "element"
        if not opts.url:
            raise ValueError("请提供网址")
        if opts.mode == "element" and not opts.selector:
            raise ValueError("请提供 CSS 选择器，例如 /元素截图 example.com #main")
        return opts

    async def _capture(self, opts: ShotOptions) -> list[bytes]:
        session = await self._get_session()
        max_height = int(self._conf("max_height", 6000))

        if opts.mode == "render":
            html = opts.url
            if not html.lstrip().lower().startswith(("<!doctype", "<html")):
                html = wrap_html(html, theme="dark" if opts.dark else "light")
            png = await session.render_html(html, opts)
        else:
            if _URL_SCHEME.match(opts.url) is None and not opts.url.startswith("//"):
                opts.url = "https://" + opts.url
            png = await session.capture(opts)

        return to_bytes(png, max_height=max_height)

    def _persist(self, event: AstrMessageEvent, payloads: list[bytes]) -> list[Path]:
        out_dir = Path(StarTools.get_data_dir("astrbot_plugin_screenshot")) / "cache"
        out_dir.mkdir(parents=True, exist_ok=True)
        session_key = re.sub(r"\W+", "_", str(event.get_session_id())) or "session"
        paths = []
        for index, data in enumerate(payloads):
            suffix = "png" if data[:4] == b"\x89PNG" else "jpg"
            target = out_dir / f"{session_key}_{index}.{suffix}"
            target.write_bytes(data)
            paths.append(target)
        return paths


HELP_TEXT = """Screenshot · CDP 直驱截图

/截图 <网址>                    整页长图
/截图 <网址> viewport           只截可视区
/截图 <网址> #main .card        截指定元素
/截图 <网址> iphone scale=2     指定设备与缩放
/截图 <网址> dark               暗色模式
/元素截图 <网址> <CSS选择器>    元素截图
/渲染截图 <html>...             渲染 HTML 片段

其它参数：wait=选择器  waitms=毫秒  hide=.广告,.浮层  watermark=水印  timeout=毫秒
设备：desktop / laptop / iphone / android / pad，也可直接写 1440x900"""
