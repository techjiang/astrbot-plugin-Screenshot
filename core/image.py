"""截图后处理：超长图切片、尺寸修正、HTML 页面包装。"""

from __future__ import annotations

import io
import textwrap

from PIL import Image

# 聊天平台对单张图片的高度普遍有上限，超过就切开发
MAX_TILE_HEIGHT = 6000
MAX_TILE_BYTES = 3 * 1024 * 1024
JPEG_QUALITY = 88


def to_bytes(
    png: bytes,
    *,
    max_height: int = MAX_TILE_HEIGHT,
    max_bytes: int = MAX_TILE_BYTES,
) -> list[bytes]:
    """把原始 PNG 规整成一组可直接发送的图片字节。

    过高的长图按 ``max_height`` 切片；仍超体积预算时重编码为 JPEG。
    """
    if max_height <= 0:
        return [png]

    with Image.open(io.BytesIO(png)) as image:
        image.load()
        width, height = image.size
        if height <= max_height and len(png) <= max_bytes:
            return [png]

        tiles: list[bytes] = []
        for top in range(0, height, max_height):
            tile = image.crop((0, top, width, min(top + max_height, height)))
            tiles.append(_encode(tile, max_bytes))
        return tiles


def _encode(image: Image.Image, max_bytes: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()
    if len(data) <= max_bytes:
        return data

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    for quality in (JPEG_QUALITY, 75, 60):
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            break
    return data


def wrap_html(body: str, *, title: str = "Screenshot", theme: str = "light") -> str:
    """给片段式 HTML 补上完整文档骨架，便于单独渲染。"""
    bg = "#1b1b1f" if theme == "dark" else "#ffffff"
    fg = "#e6e6e6" if theme == "dark" else "#1f1f1f"
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
            body {{ font: 16px/1.7 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }}
            img, svg, video, canvas {{ max-width: 100%; height: auto; }}
          </style>
        </head>
        <body>
        {body}
        </body>
        </html>
        """
    )
