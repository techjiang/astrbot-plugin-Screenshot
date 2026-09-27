"""配置解析与归一：把指令串解析成统一的截图请求。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_VIEWPORT_RE = re.compile(r"^(\d{3,5})x(\d{3,5})$")
# 带 scheme、file:// 前缀，或「主机名/路径」形状的都算 URL；
# 末尾的 #fragment 归 URL 所有，不当作 CSS 选择器
_URL_RE = re.compile(
    r"^(?:[a-z][a-z0-9+.\-]*://\S+"
    r"|localhost(?::\d+)?(?:/\S*)?$"
    r"|127\.0\.0\.1(?::\d+)?(?:/\S*)?$"
    r"|[\w\-]+(?:\.[\w\-]+)+(?::\d+)?(?:[/?#]\S*)?$)"
)
_RATIO_RE = re.compile(r"^(\d(?:\.\d)?)x$")

# 常见移动端/桌面端视口，用 key 代替一长串 WxH 参数
DEVICE_PRESETS: dict[str, dict[str, Any]] = {
    "desktop": {"viewport": (1600, 1000), "dpr": 1},
    "laptop": {"viewport": (1280, 800), "dpr": 2},
    "iphone": {"viewport": (390, 844), "dpr": 3, "mobile": True},
    "android": {"viewport": (412, 915), "dpr": 3, "mobile": True},
    "pad": {"viewport": (834, 1112), "dpr": 2, "mobile": True},
}

DEFAULT_DEVICE = "desktop"


def _looks_like_url(token: str) -> bool:
    return bool(_URL_RE.match(token))


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "开", "是"}


@dataclass
class ShotOptions:
    """一次截图请求的全部参数。"""

    url: str = ""
    mode: str = "auto"  # auto | full | viewport | element
    device: str = DEFAULT_DEVICE
    scale: float = 1.0
    mobile: bool | None = None
    selector: str = ""
    wait_for: str = ""
    wait_ms: int = 0
    timeout_ms: int = 20000
    dark: bool = False
    full_page: bool = True
    watermark: str = ""
    hide: list[str] = field(default_factory=list)

    @property
    def is_html(self) -> bool:
        return self.mode == "render"


def parse_instruction(
    raw: str,
    *,
    defaults: dict[str, Any] | None = None,
) -> ShotOptions:
    """解析形如 ``/截图 example.com full iphone scale=2`` 的指令串。

    无法识别的裸 token 会依次充当 URL 与 CSS 选择器，因此
    ``/截图 example.com "#main > .card"`` 与
    ``/截图 example.com selector=#main`` 等价。
    """
    defaults = defaults or {}
    opts = ShotOptions(
        mode=str(defaults.get("mode") or "auto"),
        device=str(defaults.get("device") or DEFAULT_DEVICE),
        full_page=parse_bool(defaults.get("full_page"), True),
        dark=parse_bool(defaults.get("dark"), False),
        timeout_ms=int(defaults.get("timeout_ms") or 20000),
    )

    # 带引号的片段整段保留，避免选择器里的空格被拆开
    tokens = re.findall(r'"([^"]*)"|\'([^\']*)\'|(\S+)', raw)
    plain = [(a or b or c) for a, b, c in tokens if (a or b or c)]

    for token in plain:
        lowered = token.lower()

        if _looks_like_url(lowered):
            opts.url = token
            continue

        if lowered.startswith("html:"):
            opts.url = token[5:]
            opts.mode = "render"
            continue

        if lowered in ("full", "fullpage", "整页", "长图"):
            opts.mode, opts.full_page = "full", True
            continue
        if lowered in ("viewport", "screen", "首屏", "可视区"):
            opts.mode, opts.full_page = "viewport", False
            continue
        if lowered in ("render", "html", "渲染"):
            opts.mode = "render"
            continue
        if lowered in ("dark", "夜间", "暗色"):
            opts.dark = True
            continue

        ratio = _RATIO_RE.match(lowered)
        if ratio:
            opts.scale = float(ratio.group(1))
            continue

        viewport = _VIEWPORT_RE.match(lowered)
        if viewport:
            opts.device = lowered
            continue

        if lowered in DEVICE_PRESETS:
            opts.device = lowered
            continue

        if "=" in token:
            key, _, value = token.partition("=")
            _apply_kv(opts, key.strip().lower(), value.strip())
            continue

        if not opts.url:
            opts.url = token
        elif not opts.selector:
            opts.selector = token

    if opts.selector and opts.mode == "auto":
        opts.mode = "element"
    if opts.mode == "auto":
        opts.mode = "full" if opts.full_page else "viewport"
    return opts


def _apply_kv(opts: ShotOptions, key: str, value: str) -> None:
    if key in ("scale", "dpr", "zoom"):
        opts.scale = float(value)
    elif key in ("device", "viewport"):
        opts.device = value.lower()
    elif key in ("mobile", "h5"):
        opts.mobile = parse_bool(value)
    elif key in ("dark", "theme"):
        opts.dark = parse_bool(value) or value.lower() == "dark"
    elif key in ("full", "fullpage", "full_page"):
        opts.full_page = parse_bool(value)
        opts.mode = "full" if opts.full_page else "viewport"
    elif key in ("selector", "el", "element", "选择器"):
        opts.selector = value
        opts.mode = "element"
    elif key in ("wait", "wait_for"):
        opts.wait_for = value
    elif key in ("waitms", "delay"):
        opts.wait_ms = int(value)
    elif key in ("timeout", "timeout_ms"):
        opts.timeout_ms = int(value)
    elif key in ("hide", "remove"):
        opts.hide.extend(part for part in value.split(",") if part)
    elif key in ("watermark", "mark"):
        opts.watermark = value


def viewport_for(device: str) -> tuple[tuple[int, int], float, bool]:
    """返回 ``(宽高, deviceScaleFactor, 是否移动端)``。"""
    preset = DEVICE_PRESETS.get(device)
    if preset:
        return preset["viewport"], preset["dpr"], bool(preset.get("mobile"))

    match = _VIEWPORT_RE.match(device)
    if match:
        return (int(match.group(1)), int(match.group(2))), 2, False

    fallback = DEVICE_PRESETS[DEFAULT_DEVICE]
    return fallback["viewport"], fallback["dpr"], bool(fallback.get("mobile"))
