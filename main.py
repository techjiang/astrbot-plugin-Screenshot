"""Screenshot —— 新一代非 API 接口 AstrBot 截图插件。

插件只通过 CDP（Chrome DevTools Protocol）驱动浏览器内核，不引入
Playwright / Selenium / html2image 这类封装库，因此没有浏览器驱动版本漂移问题。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

from .core.browser import find_browser
from .core.config import (
    ShotOptions,
    as_float,
    as_int,
    load_extra_headers,
    parse_instruction,
    suggest_url,
)
from .core.image import detect_mime, suggest_suffix, to_bytes, wrap_html
from .core.session import ScreenshotError, ScreenshotSession


# 缓存文件最多保留这么多张，避免长期运行把磁盘写满
CACHE_KEEP = 120


class ScreenshotPlugin(Star):
    """Screenshot · CDP 直驱截图

    /截图 <网址> [ viewport | full | 元素选择器 ] [设备] [scale=2] [dark] \\
[hide=.a,.b] [watermark=文本] [wait=选择器] [waitms=毫秒] [format=png|jpeg|pdf]
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
            self._session = ScreenshotSession(
                binary,
                self._conf("launch_flags", []),
                proxy=str(self._conf("proxy", "") or ""),
                headers=load_extra_headers(self.config),
                max_concurrent=as_int(self._conf("max_concurrent", 4), 4, low=1, high=16),
                max_dpr=as_float(self._conf("max_dpr", 0), 0.0, low=0.0, high=4.0),
                render_watch_ms=as_int(
                    self._conf("render_watch_ms", 250), 250, low=0, high=5000
                ),
            )
        return self._session

    async def terminate(self) -> None:
        if self._session:
            await self._session.shutdown()
            self._session = None
        self._cleanup_cache()

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

    @filter.command("截图帮助")
    async def help_command(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    # ---------- 实现 ----------

    async def _shoot(self, event: AstrMessageEvent, instruction: str, mode: str):
        instruction = (instruction or "").strip()
        if not instruction:
            yield event.plain_result(HELP_TEXT)
            return

        try:
            opts = self._build_options(instruction, mode)
            payloads = await self._capture(opts)
        except ValueError as exc:
            yield event.plain_result(f"参数有误：{exc}")
            return
        except ScreenshotError as exc:
            logger.warning("截图失败：%s", exc)
            yield event.plain_result(f"截图失败：{exc}")
            return
        except Exception as exc:  # 兜底：不让异常逃逸成静默无响应
            logger.exception("截图出现未预期错误")
            yield event.plain_result(f"截图失败：{type(exc).__name__}: {exc}")
            return

        if not payloads:
            yield event.plain_result("截图失败：未生成任何图片")
            return

        for path in self._persist(event, payloads):
            if detect_mime(path.read_bytes()).startswith("image/"):
                yield event.image_result(str(path))
            else:
                yield event.plain_result(f"已生成文件：{path.name}")

    def _build_options(self, instruction: str, mode: str) -> ShotOptions:
        if mode == "render":
            opts = ShotOptions(mode="render")
            opts.url = instruction[5:].lstrip() if instruction.lower().startswith("html:") \
                else instruction
            if not opts.url.strip():
                raise ValueError("请提供要渲染的 HTML，例如 /渲染截图 <h1>你好</h1>")
            opts.timeout_ms = as_int(self._conf("timeout_ms", 20000), 20000,
                                     low=1000, high=600000)
            return opts

        opts = parse_instruction(instruction, defaults=self.config)
        if mode == "element":
            opts.mode = "element"
            opts.selector = opts.selector or ""
        if not opts.url:
            raise ValueError("请提供网址，例如 /截图 example.com")
        # 元素模式必须先校验选择器，否则 `suggest_url` 会把
        # `example.com #main` 里被空格切开的碎片拼成 URL，报出莫名其妙的域名错
        if opts.mode == "element" and not opts.selector.strip():
            raise ValueError("请提供 CSS 选择器，例如 /元素截图 example.com #main")
        opts.url = suggest_url(opts.url)
        if opts.mode == "element" and not opts.selector:
            raise ValueError("请提供 CSS 选择器，例如 /元素截图 example.com #main")
        return opts

    async def _capture(self, opts: ShotOptions) -> list[bytes]:
        session = await self._get_session()
        # 区分「没写 max_height」（跟随插件配置）与「显式写了 0」（本次不切片）：
        # 老实现用 ``opts.max_height or ...``，用户敲 max_height=0 会被当成没填，
        # 仍然按 6000 切片，指令形同虚设。
        if opts.max_height_explicit:
            max_height = max(0, opts.max_height)
        else:
            max_height = as_int(self._conf("max_height", 6000), 6000, low=0, high=100000)

        if opts.mode == "render":
            html = opts.url
            if not html.lstrip().lower().startswith(("<!doctype", "<html")):
                html = wrap_html(html, theme="dark" if opts.dark else "light")
            png = await session.render_html(html, opts)
        else:
            png = await session.capture(opts)

        return to_bytes(
            png,
            max_height=max_height,
            fmt=opts.img_format,
            quality=opts.quality,
        )

    def _cache_dir(self) -> Path:
        out_dir = Path(StarTools.get_data_dir("astrbot_plugin_screenshot")) / "cache"
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    def _persist(self, event: AstrMessageEvent, payloads: list[bytes]) -> list[Path]:
        out_dir = self._cache_dir()
        session_key = re.sub(r"\W+", "_", str(event.get_session_id())) or "session"
        paths: list[Path] = []
        stamp = f"{time.time_ns():x}"
        for index, data in enumerate(payloads):
            suffix = suggest_suffix(data)
            target = out_dir / f"{session_key}_{stamp}_{index}{suffix}"
            target.write_bytes(data)
            paths.append(target)
        # 本次产物列入保护名单，避免同会话连续截图时被清理误删
        self._cleanup_cache(out_dir, keep=CACHE_KEEP, protect=set(paths))
        return paths

    def _cleanup_cache(
        self,
        out_dir: Path | None = None,
        *,
        keep: int = CACHE_KEEP,
        protect: set[Path] | None = None,
    ) -> None:
        """按修改时间清理历史缓存，失败不影响主流程。

        ``protect`` 里的文件（通常是本次刚写出的产物）永不被删：
        调用方拿到的是路径，若刚写完就被清掉，会读到不存在的文件。
        """
        try:
            directory = out_dir or self._cache_dir()
            protected = protect or set()
            files = sorted(
                (p for p in directory.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for stale in files[keep:]:
                if stale in protected:
                    continue
                stale.unlink(missing_ok=True)
        except Exception as exc:  # 清理是尽力而为
            logger.debug("缓存清理跳过：%s", exc)


HELP_TEXT = """Screenshot · CDP 直驱截图

/截图 <网址>                      整页长图
/截图 <网址> viewport             只截可视区
/截图 <网址> #main .card          截指定元素
/截图 <网址> iphone scale=2       设备与缩放
/截图 <网址> dark                 暗色模式
/截图 <网址> transparent          透明背景（PNG 带 alpha，适合 Logo/图标）
/截图 <网址> padding=24           元素截图向外留白（容下阴影与描边）
/截图 <网址> format=jpeg quality=80  指定输出格式
/截图 <网址> format=pdf           输出 PDF，适合超长页面
/元素截图 <网址> <CSS选择器>      元素截图
/渲染截图 <html>...               渲染 HTML 片段
/截图帮助                         查看用法

参数：wait=选择器  waitms=毫秒  hide=.广告,.浮层  watermark=水印
      timeout=毫秒  padding=像素  transparent  max_height=像素(0=不切)
设备：desktop / laptop / iphone / android / pad，也可直接写 1440x900

值里要带空格或引号时用引号包起来，内部引号用反斜杠转义：
/截图 example.com watermark="科技酱 官方"
"""
