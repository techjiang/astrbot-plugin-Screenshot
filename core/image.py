"""截图后处理：超长图切片、重编码、PDF 封装、HTML 包装。"""

from __future__ import annotations

import io
import textwrap

from PIL import Image

# 聊天平台对单张图片的高度普遍有上限，超过就切开发
MAX_TILE_HEIGHT = 6000
MAX_TILE_BYTES = 3 * 1024 * 1024
JPEG_QUALITY = 88
MAX_SCALE = 4  # 超过 4 倍 DPR 后 CDP 会开始掉帧，收益也接近零

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
GIF_MAGIC = b"GIF8"
WEBP_MAGIC = b"RIFF"
PDF_MAGIC = b"%PDF"

MAGIC_TYPES = (
    (PNG_MAGIC, "image/png"),
    (JPEG_MAGIC, "image/jpeg"),
    (GIF_MAGIC, "image/gif"),
    (PDF_MAGIC, "application/pdf"),
    (WEBP_MAGIC, "image/webp"),
)


def detect_mime(data: bytes) -> str:
    """按文件头判断 MIME，不依赖扩展名。"""
    for magic, mime in MAGIC_TYPES:
        if data.startswith(magic):
            return mime
    return "application/octet-stream"


def suggest_suffix(data: bytes) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
    }.get(detect_mime(data), ".bin")


def normalise_scale(scale: float) -> float:
    """DPR 归一：太小看不清、太大只会让 CDP 变慢。"""
    try:
        value = float(scale)
    except (TypeError, ValueError):
        return 1.0
    if value != value or value <= 0:  # NaN / 非正数
        return 1.0
    return round(max(0.2, min(value, MAX_SCALE)), 2)


def to_bytes(
    png: bytes,
    *,
    max_height: int = MAX_TILE_HEIGHT,
    max_bytes: int = MAX_TILE_BYTES,
    fmt: str = "png",
    quality: int = JPEG_QUALITY,
) -> list[bytes]:
    """把原始 PNG 规整成一组可直接发送的图片字节。

    ``fmt`` 为 ``png`` / ``jpeg`` 时按格式编码，为 ``pdf`` 时直接把整页塞进 PDF
    （PDF 自身没有平台高度上限，因此不做切片）。
    图片过高时按 ``max_height`` 切片；仍超体积预算时重编码为 JPEG。
    """
    if fmt in ("pdf", "application/pdf"):
        return [to_pdf(png)]

    quality = int(quality) if 10 <= int(quality) <= 100 else JPEG_QUALITY

    try:
        with Image.open(io.BytesIO(png)) as probe:
            probe.load()
            width, height = probe.size
            image: Image.Image | None = probe.copy()
    except (OSError, ValueError):  # 拿到的不是图片时原样返回，避免整条链路崩掉
        return [png]

    try:
        assert image is not None
        if fmt in ("jpeg", "jpg"):
            return _tile_jpeg(image, height, max_height, max_bytes, quality)

        has_alpha = image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info)

        # 透明图必须留 PNG：切成 JPEG 会把 alpha 压成白底，「无背景 Logo」就毁了
        if height <= max(1, max_height) and len(png) <= max_bytes:
            return [png]

        tiles: list[bytes] = []
        step = max(1, int(max_height)) if max_height > 0 else height
        for top, size in plan_tiles(height, step):
            tile = image.crop((0, top, width, top + size))
            if fmt in ("jpeg", "jpg"):
                tiles.append(_encode_jpeg(tile, max_bytes, quality))
            else:
                tiles.append(_encode_png(tile, max_bytes, quality, keep_alpha=has_alpha))
        return tiles
    finally:
        if image is not None:
            image.close()


def plan_tiles(height: int, step: int) -> list[tuple[int, int]]:
    """把总高 ``height`` 按 ``step`` 规划成若干切片，返回 ``(top, size)``。

    与 ``session._plan_strips`` 同样的动机：直接 ``range(0, height, step)`` 会
    在末尾留下碎片 —— 实测 84016px 按 6000px 切，最后一片只有 16px。后果是
    水印（贴文档底 ``height-34``）落进倒数第二片，看起来像「水印跑到画面中部」，
    聊天里还多一张 16px 的废图。

    这里按片数均分，让每片尽量等长，末片不会再退化成碎片。
    """
    if height <= step or step <= 0:
        return [(0, max(0, height))]
    count = -(-height // step)
    size = -(-height // count)
    tiles: list[tuple[int, int]] = []
    top = 0
    while top < height:
        part = min(size, height - top)
        tiles.append((top, part))
        top += part
    return tiles


def _tile_jpeg(
    image: Image.Image, height: int, max_height: int, max_bytes: int, quality: int
) -> list[bytes]:
    step = max(1, int(max_height)) if max_height > 0 else height
    if height <= step:
        return [_encode_jpeg(image, max_bytes, quality)]
    return [
        _encode_jpeg(image.crop((0, top, image.width, top + size)), max_bytes, quality)
        for top, size in plan_tiles(height, step)
    ]


def _encode_png(
    image: Image.Image, max_bytes: int, quality: int, *, keep_alpha: bool = False
) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()
    if len(data) <= max_bytes:
        return data
    if keep_alpha:
        # 透明图不降级成 JPEG（会把 alpha 变成白底），改为再压一档
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True, compress_level=9)
        return buffer.getvalue()
    # PNG 压不下去时退化为 JPEG，聊天平台普遍接受
    return _encode_jpeg(image, max_bytes, quality)


def _encode_jpeg(image: Image.Image, max_bytes: int, quality: int) -> bytes:
    if image.mode not in ("RGB", "L"):
        if image.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            converted = image.convert("RGBA")
            background.paste(converted, mask=converted.split()[-1])
            converted.close()
            image = background
        else:
            image = image.convert("RGB")

    data = b""
    for level in (quality, min(quality, 75), 60, 45):
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=level, optimize=True)
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            break
    return data


def to_pdf(png: bytes, *, max_pages: int = 50) -> bytes:
    """把长图折成多页 PDF。

    先按「一屏一段」把长图切成若干段，再把每段等比压进一张 A4（宽图自动转横向）。

    为什么不直接按 A4 高度硬裁：那样切点由像素高度决定，**与内容无关**，实测
    一页 842px 的切点正好落在一行文字的中间，字被横向切成两半。按段分页后，
    每页之间有明确边界，正文不会被腰斩。
    """
    with Image.open(io.BytesIO(png)) as source:
        source.load()
        image = source.convert("RGB")

    try:
        page_w, page_h = 595, 842  # A4 @72dpi
        if image.width / max(image.height, 1) > 0.9:
            page_w, page_h = 842, 595

        scaled_h = max(1, round(image.height * page_w / max(image.width, 1)))
        resized = image.resize((page_w, scaled_h), Image.LANCZOS)
        try:
            pages = _paginate(resized, page_w, page_h, max_pages)
            try:
                buffer = io.BytesIO()
                first, rest = pages[0], pages[1:]
                first.save(buffer, format="PDF", resolution=72.0, save_all=True,
                           append_images=rest)
                return buffer.getvalue()
            finally:
                for page in pages:
                    page.close()
        finally:
            resized.close()
    finally:
        image.close()


def _paginate(
    image: Image.Image, page_w: int, page_h: int, max_pages: int
) -> list[Image.Image]:
    """把整页高的图按「一段一页」切开，每段再等比缩放到页宽。

    ``seg`` 取页高，所以每页恰好铺满高度；最后一段不足页高时按比例缩放到
    页宽后也不会被拉变形（保持宽高比，只对齐宽度）。
    """
    if image.height <= page_h:
        # 本来就装得下一页：直接补白居中，不做无意义的重采样
        canvas = Image.new("RGB", (page_w, page_h), (255, 255, 255))
        canvas.paste(image, (0, 0))
        return [canvas]

    # 段高取「总高均分到不超过 max_pages 页」——避免固定 page_h 时末页只剩几个像素
    count = min(max_pages, -(-image.height // page_h))
    seg = -(-image.height // count)
    pages: list[Image.Image] = []
    for top in range(0, image.height, seg):
        tile = image.crop((0, top, page_w, min(top + seg, image.height)))
        canvas = Image.new("RGB", (page_w, page_h), (255, 255, 255))
        if tile.height >= page_h:
            canvas.paste(tile, (0, 0))
        else:
            # 段高不足一页时，按「填满页宽」等比缩放后贴顶：只对齐宽度，不拉伸变形
            ratio = page_w / max(tile.width, 1)
            scaled = tile.resize(
                (page_w, max(1, round(tile.height * ratio))), Image.LANCZOS
            )
            try:
                canvas.paste(scaled, (0, 0))
            finally:
                scaled.close()
        tile.close()
        pages.append(canvas)
        if len(pages) >= max_pages:
            break
    if not pages:
        pages = [Image.new("RGB", (page_w, page_h), (255, 255, 255))]
    return pages


def wrap_html(body: str, *, title: str = "Screenshot", theme: str = "light") -> str:
    """给片段式 HTML 补上完整文档骨架，便于单独渲染。"""
    dark = theme == "dark"
    bg = "#1b1b1f" if dark else "#ffffff"
    fg = "#e6e6e6" if dark else "#1f1f1f"
    title = (title or "Screenshot").replace("<", "&lt;").replace(">", "&gt;")
    return textwrap.dedent(
        f"""\
        <!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>{title}</title>
          <style>
            html, body {{ margin: 0; background: {bg}; color: {fg}; }}
            body {{ font: 16px/1.7 -apple-system, BlinkMacSystemFont, "PingFang SC",
                    "Hiragino Sans GB", "Noto Sans CJK SC", "Source Han Sans SC",
                    "Microsoft YaHei", sans-serif; }}
            img, svg, video, canvas {{ max-width: 100%; height: auto; }}
            pre, code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas,
                         "Noto Sans Mono CJK SC", monospace; }}
          </style>
        </head>
        <body>
        {body}
        </body>
        </html>
        """
    )
