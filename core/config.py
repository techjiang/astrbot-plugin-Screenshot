"""配置解析与归一：把指令串解析成统一的截图请求。"""

from __future__ import annotations

import posixpath
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
_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]|^[\\/]")
# 形如 selector=#a > .b 里带空格的取值，以及被空格切开的 key=value 片段
_KV_RE = re.compile(r"^([A-Za-z_][\w\-]*)=(.*)$")

# 常见移动端/桌面端视口，用 key 代替一长串 WxH 参数
DEVICE_PRESETS: dict[str, dict[str, Any]] = {
    "desktop": {"viewport": (1600, 1000), "dpr": 1},
    "laptop": {"viewport": (1280, 800), "dpr": 2},
    "iphone": {"viewport": (390, 844), "dpr": 3, "mobile": True},
    "android": {"viewport": (412, 915), "dpr": 3, "mobile": True},
    "pad": {"viewport": (834, 1112), "dpr": 2, "mobile": True},
}
DEVICE_ALIASES = {
    "pc": "desktop",
    "电脑": "desktop",
    "笔记本": "laptop",
    "手机": "iphone",
    "平板": "pad",
    "ipad": "pad",
}

DEFAULT_DEVICE = "desktop"
# 截图模式白名单，防止指令串里写错模式名时静默降级
SHOT_MODES = ("auto", "full", "viewport", "element", "render")
IMAGE_FORMATS = ("png", "jpeg")


def _looks_like_url(token: str) -> bool:
    return bool(_URL_RE.match(token))


def _looks_like_path(token: str) -> bool:
    return bool(_PATH_RE.match(token))


def parse_bool(value: Any, default: bool = False) -> bool:
    """宽松布尔解析：认 ``1/true/yes/on/开/是``，也认 ``d=0`` 这类写法。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "开", "是", ""}:
        return True
    if text in {"0", "false", "no", "n", "off", "关", "否"}:
        return False
    return default


def as_int(value: Any, default: int, *, low: int | None = None,
           high: int | None = None) -> int:
    """把配置/参数转成整数；非法值回退默认，超范围自动夹取。"""
    try:
        result = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    if low is not None:
        result = max(low, result)
    if high is not None:
        result = min(high, result)
    return result


def as_float(value: Any, default: float, *, low: float | None = None,
             high: float | None = None) -> float:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    if low is not None:
        result = max(low, result)
    if high is not None:
        result = min(high, result)
    return result


def load_extra_headers(defaults: dict[str, Any] | None) -> dict[str, str]:
    """读取配置里的自定义请求头，兼容 dict 与 ``A: B`` 多行字符串两种写法。"""
    if not defaults:
        return {}
    raw = defaults.get("headers") or defaults.get("extra_headers")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if str(k).strip()}
    headers: dict[str, str] = {}
    for line in str(raw).splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip():
            headers[name.strip()] = value.strip()
    return headers


@dataclass
class ShotOptions:
    """一次截图请求的全部参数。"""

    url: str = ""
    mode: str = "auto"  # auto | full | viewport | element | render
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
    img_format: str = "png"
    quality: int = 88
    max_height: int = 0  # 0 表示交给插件级配置决定
    # 用户是否显式写了 max_height（写了 0 就是「本次不切片」，不能再被配置覆盖）
    max_height_explicit: bool = False
    light: bool = False  # 渲染 HTML 时套用浅色骨架
    print_media: bool = False  # 是否切到 print 媒体查询
    padding: int = 0  # 元素截图向外扩的留白（px），用来容下阴影/描边/圆角光晕
    transparent: bool = False  # 保留页面透明背景（输出 PNG 时才有意义）

    @property
    def is_html(self) -> bool:
        return self.mode == "render"


# 引号内的取值允许反斜杠转义（``watermark="a \" b"``），否则整段会被切成碎片
_TOKEN_RE = re.compile(
    r"""([A-Za-z_][\w\-]*)=\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|([^\s]+))"""
    r"""|"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|(\S+)"""
)

_ESCAPE_RE = re.compile(r'\\([\\"\'])')


def _unescape(value: str) -> str:
    """还原引号内的 ``\\"`` / ``\\'`` / ``\\\\``，让用户能表达带引号的水印与选择器。"""
    return _ESCAPE_RE.sub(r"\1", value) if value else value


def _tokenize_classic(raw: str) -> list[str]:
    """引号/反斜杠不配对时的兜底切分。"""
    pieces: list[str] = []
    for chunk in raw.split():
        if pieces and (pieces[-1].endswith(("\\", "=")) or pieces[-1].count('"') % 2
                       or pieces[-1].count("'") % 2):
            pieces[-1] = pieces[-1] + " " + chunk
        else:
            pieces.append(chunk)
    return pieces


def tokenize(raw: str) -> list[str]:
    """把指令串切成 token。

    单双引号内的整段保留；``key="值 含空格"`` 与 ``key=值 含空格`` 都会还原成
    带空格的单个 token，选择器与 HTML 片段不会被参数分隔符切碎。
    """
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(raw):
        key, dq, sq, bare, dq2, sq2, word = match.groups()
        if key:
            value = dq if dq is not None else (sq if sq is not None else bare)
            tokens.append(f"{key}={_unescape(value)}")
        else:
            piece = dq2 if dq2 is not None else (sq2 if sq2 is not None else word)
            if piece:
                tokens.append(_unescape(piece))
    if not tokens or _has_unbalanced_quote(raw):
        return _tokenize_classic(raw)
    return tokens


def _has_unbalanced_quote(raw: str) -> bool:
    """判断引号是否没配对（转义引号不算数）。"""
    for quote in ('"', "'"):
        count = 0
        escaped = False
        for char in raw:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == quote:
                count += 1
        if count % 2:
            return True
    return False


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
        timeout_ms=as_int(defaults.get("timeout_ms"), 20000, low=1000, high=600000),
        img_format=str(defaults.get("image_format") or "png").lower(),
        quality=as_int(defaults.get("jpeg_quality"), 88, low=10, high=100),
        transparent=parse_bool(defaults.get("transparent"), False),
        padding=as_int(defaults.get("padding"), 0, low=0, high=400),
    )

    for token in tokenize(raw):
        lowered = token.lower()

        if _looks_like_url(lowered) or _looks_like_path(token):
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
        if lowered in ("light", "亮色", "浅色"):
            opts.light = True
            continue
        if lowered in ("print", "打印", "打印样式"):
            opts.print_media = True
            continue
        if lowered in ("pdf",):
            opts.img_format = "pdf"
            continue
        if lowered in ("transparent", "透明", "透明背景", "alpha"):
            opts.transparent = True
            continue

        ratio = _RATIO_RE.match(lowered)
        if ratio:
            opts.scale = as_float(ratio.group(1), 1.0, low=0.2, high=4)
            continue

        viewport = _VIEWPORT_RE.match(lowered)
        if viewport:
            opts.device = lowered
            continue

        if lowered in DEVICE_PRESETS:
            opts.device = lowered
            continue
        if lowered in DEVICE_ALIASES:
            opts.device = DEVICE_ALIASES[lowered]
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
    if opts.mode not in SHOT_MODES:
        opts.mode = "full" if opts.full_page else "viewport"
    if opts.img_format not in IMAGE_FORMATS + ("pdf",):
        opts.img_format = "png"
    if opts.light:
        opts.dark = False
    return opts


def _apply_kv(opts: ShotOptions, key: str, value: str) -> None:
    if key in ("scale", "dpr", "zoom"):
        opts.scale = as_float(value, opts.scale, low=0.2, high=4)
    elif key in ("device", "viewport"):
        opts.device = DEVICE_ALIASES.get(value.lower(), value.lower())
    elif key in ("print", "打印", "media"):
        opts.print_media = parse_bool(value) or value.lower() == "print"
    elif key in ("mobile", "h5"):
        opts.mobile = parse_bool(value)
    elif key in ("dark", "theme"):
        if value.lower() in ("dark", "暗色", "夜间"):
            opts.dark = True
        elif value.lower() in ("light", "亮色", "浅色"):
            opts.dark = False
        else:
            opts.dark = parse_bool(value, opts.dark)
    elif key in ("full", "fullpage", "full_page"):
        opts.full_page = parse_bool(value, opts.full_page)
        opts.mode = "full" if opts.full_page else "viewport"
    elif key in ("selector", "el", "element", "选择器"):
        opts.selector = value
        opts.mode = "element"
    elif key in ("wait", "wait_for"):
        opts.wait_for = value
    elif key in ("waitms", "delay"):
        opts.wait_ms = as_int(value, 0, low=0, high=60000)
    elif key in ("timeout", "timeout_ms"):
        opts.timeout_ms = as_int(value, opts.timeout_ms, low=1000, high=600000)
    elif key in ("hide", "remove"):
        opts.hide.extend(part.strip() for part in value.split(",") if part.strip())
    elif key in ("watermark", "mark"):
        opts.watermark = value
    elif key in ("format", "图片格式"):
        fmt = value.lower()
        if fmt in ("jpg", "jpeg", "png", "pdf"):
            opts.img_format = "jpeg" if fmt == "jpg" else fmt
    elif key in ("quality", "q", "质量"):
        opts.quality = as_int(value, opts.quality, low=10, high=100)
    elif key in ("max_height", "maxheight", "切片高度"):
        opts.max_height = as_int(value, 0, low=0, high=100000)
        opts.max_height_explicit = True
    elif key in ("padding", "pad", "留白", "边距"):
        opts.padding = as_int(value, opts.padding, low=0, high=400)
    elif key in ("transparent", "透明", "alpha", "bg"):
        opts.transparent = parse_bool(value, opts.transparent) or value.lower() in (
            "transparent", "透明", "none")


def viewport_for(device: str) -> tuple[tuple[int, int], float, bool]:
    """返回 ``(宽高, deviceScaleFactor, 是否移动端)``。"""
    preset = DEVICE_PRESETS.get(device)
    if preset:
        return preset["viewport"], preset["dpr"], bool(preset.get("mobile"))

    match = _VIEWPORT_RE.match(device.strip().lower())
    if match:
        return (int(match.group(1)), int(match.group(2))), 2, False

    fallback = DEVICE_PRESETS[DEFAULT_DEVICE]
    return fallback["viewport"], fallback["dpr"], bool(fallback.get("mobile"))


def suggest_url(token: str, base: str = "") -> str:
    """把用户输入整理成可导航的 URL。

    裸域名补 ``https://``；本机地址退 ``http://``；相对路径按 ``base`` 补全；
    ``file://`` 等已带 scheme 的原样返回。
    """
    token = token.strip()
    if not token:
        return token
    if _URL_RE.match(token) and "://" in token:
        return token
    if _looks_like_path(token):
        return token
    if base and not token.startswith(("http://", "https://")):
        scheme, _, rest = base.partition("://")
        if rest and scheme in ("http", "https"):
            host = rest.split("/", 1)[0]
            path = rest.split("/", 1)[1] if "/" in rest else ""
            directory = posixpath.dirname("/" + path) if path else "/"
            joined = posixpath.normpath(posixpath.join(directory, token))
            return f"{scheme}://{host}{joined}"

    host = token.split("/", 1)[0].split(":", 1)[0].lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local"):
        scheme = "http"
    else:
        scheme = "https"
    return f"{scheme}://{token}"
