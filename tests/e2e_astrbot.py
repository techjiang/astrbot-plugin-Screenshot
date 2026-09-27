"""端到端实测：用 AstrBot 真实的 PluginManager 加载插件，走真 CDP + 真 Chromium。

本地运行（需要系统里有 chromium）：

    ASTRBOT_ROOT=/tmp/ab python3 tests/e2e_astrbot.py

会依次跑完 tests/cases.json 里的用例，把产物落到 ``$ASTRBOT_ROOT/data/plugin_data``
下的 cache 目录，并在 stdout 打出 OK/FAIL 汇总。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent

ROOT = Path(os.environ.setdefault("ASTRBOT_ROOT", "/tmp/ab"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PLUGIN_ROOT.parent))

import astrbot.api as api  # noqa: E402
from astrbot.api.platform import MessageType  # noqa: E402
from astrbot.core.config import AstrBotConfig  # noqa: E402
from astrbot.core.message.components import Plain  # noqa: E402
from astrbot.core.star.context import Context  # noqa: E402
from astrbot.core.star.star_manager import PluginManager  # noqa: E402

RESULTS: list[dict] = []
SITE_PORT = int(os.environ.get("SHOT_SITE_PORT", "8899"))  # main() 起站点时会自动改


class _Stub:
    def __getattr__(self, name):
        return _Stub()

    def __call__(self, *a, **k):
        return _Stub()


class FakePlatform:
    id = "test_adapter"
    name = "test"

    def meta(self):
        return None


class FakeMessageObj:
    type = MessageType.GROUP_MESSAGE
    group_id = "10001"
    message_id = "m1"

    def __init__(self, text: str) -> None:
        self.message_str = text
        self.message = [Plain(text=text)]
        self.sender = type("S", (), {"user_id": "10001", "nickname": "tester"})()
        self.self_id = "bot"
        self.session_id = "test:group:10001"


class FakeEvent(api.event.AstrMessageEvent):
    def __init__(self, text: str) -> None:
        super().__init__(text, FakeMessageObj(text), FakePlatform(), "test_group_10001")
        self.results: list[tuple[str, str]] = []

    def get_session_id(self):
        return "test_group_10001"

    def get_sender_id(self):
        return "10001"

    def get_self_id(self):
        return "bot"

    def plain_result(self, text):
        self.results.append(("text", text))
        return "TEXT:" + text

    def image_result(self, path):
        self.results.append(("image", str(path)))
        return "IMG:" + str(path)


def _make_context(cfg):
    ctx = Context.__new__(Context)
    ctx._star_manager = None
    ctx._config = cfg
    for attr in ("_event_queue", "_db", "_provider_manager", "_platform_manager",
                 "_conversation_manager", "_message_history_manager", "_persona_manager",
                 "_astrbot_config_mgr", "_knowledge_base_manager", "_cron_manager",
                 "_event_bus"):
        try:
            setattr(ctx, attr, _Stub())
        except Exception:
            pass
    return ctx



def _find_watermark_bottom(image_path: str) -> int | None:
    """在图片最右侧区域找出水印灰胶囊的底部 y 坐标。

    水印是一块**实心**半透明黑胶囊：在某一行里会连续铺满整块宽度。
    正文文字同样是中性灰，但每行只有零散笔画、且字高远小于胶囊宽度，
    所以这里要求「连续游程达到胶囊宽度」才认，避免把正文误判成水印。
    """
    import numpy as np
    from PIL import Image

    with Image.open(image_path) as handle:
        arr = np.asarray(handle.convert("RGB")).astype(int)
    width = arr.shape[1]
    band = max(320, int(width * 0.12))
    right = arr[:, max(0, width - band):, :]
    neutral = (abs(right[:, :, 0] - right[:, :, 1]) < 14) & (
        abs(right[:, :, 1] - right[:, :, 2]) < 14
    )
    darker = (right[:, :, 0] > 70) & (right[:, :, 0] < 205)
    mask = neutral & darker

    # 胶囊宽度约 80~260px（随 DPR 放大）；正文文字的单行连续游程通常 < 20px。
    # 取 55px 作为门槛，既能认出胶囊又不会把正文笔画算进来。
    min_run = 55
    rows: list[int] = []
    for y in range(arr.shape[0]):
        xs = np.where(mask[y])[0]
        if len(xs) < min_run:
            continue
        run = best = 1
        for index in range(1, len(xs)):
            run = run + 1 if xs[index] - xs[index - 1] <= 2 else 1
            best = max(best, run)
        if best >= min_run:
            rows.append(y)
    if not rows:
        return None
    groups: list[tuple[int, int]] = []
    start = prev = rows[0]
    for y in rows[1:]:
        if y - prev <= 6:
            prev = y
        else:
            groups.append((start, prev))
            start = prev = y
    groups.append((start, prev))
    # 胶囊高度视 DPR 在 20~100px 之间；正文段落（若恰好命中）会明显更厚
    plausible = [g for g in groups if 12 <= g[1] - g[0] <= 110]
    return max((g[1] for g in plausible), default=None)


def _check_assertions(case: dict, images: list[str], texts: list[str]) -> bool:
    """对产物做像素级断言，避免「跑通但结果不对」被漏判。"""
    for rule in case.get("assert", []):
        kind = rule["kind"]
        if kind == "min_fragments" and len(images) < rule["value"]:
            print(f"      ↳ 断言失败：切片数 {len(images)} < {rule['value']}")
            return False
        if kind == "image_size":
            from PIL import Image

            with Image.open(images[rule.get("index", 0)]) as handle:
                if list(handle.size) != rule["value"]:
                    print(f"      ↳ 断言失败：尺寸 {handle.size} != {rule['value']}")
                    return False
        if kind == "watermark_at_bottom":
            # 水印只应出现在最后一片的底部；前面的切片里出现即视为跑到画面中部
            from PIL import Image

            within = rule["within"]
            for index, path in enumerate(images):
                bottom = _find_watermark_bottom(path)
                if bottom is None:
                    continue
                with Image.open(path) as handle:
                    height = handle.height
                is_last = index == len(images) - 1
                if not is_last:
                    print(f"      ↳ 断言失败：水印出现在第 {index + 1} 片 y={bottom}，"
                          f"应只在最后一片（共 {len(images)} 片）")
                    return False
                if bottom < height - within:
                    print(f"      ↳ 断言失败：水印在最后一片 y={bottom}，"
                          f"未贴到图底（片高 {height}）")
                    return False
            else:
                if _find_watermark_bottom(images[-1]) is None:
                    print("      ↳ 断言失败：整页图里找不到水印")
                    return False
        if kind == "bottom_band":
            # 页面底部的固定浮层必须出现在**图底**，不能跑到画面中部
            from PIL import Image

            target = rule["value"]
            tolerance = rule.get("tolerance", 12)
            within = rule.get("within", 260)
            path = images[rule.get("index", -1)]
            with Image.open(path) as handle:
                image = handle.convert("RGB")
            pixels = image.load()
            width, height = image.size
            found = None
            for y in range(height - 1, -1, -1):
                row = [pixels[x, y] for x in range(width // 2, width)]
                hit = sum(
                    1 for c in row
                    if abs(c[0] - target[0]) < tolerance
                    and abs(c[1] - target[1]) < tolerance
                    and abs(c[2] - target[2]) < tolerance
                )
                # 浮层只占右下角一块：半幅宽度里至少命中 8 个采样点即认定
                if hit >= 8:
                    found = y
                    break
            if found is None:
                print(f"      ↳ 断言失败：整页图里找不到图底固定浮层 {target}")
                return False
            if found < height - within:
                print(f"      ↳ 断言失败：图底固定浮层在 y={found}，"
                      f"图高 {height}，未贴到图底（阈值 {within}）")
                return False
        if kind == "color_present":
            # 断言某颜色确实出现在图里（用于「print 媒体下才出现的标记」这类）
            import numpy as np
            from PIL import Image

            with Image.open(images[0]) as handle:
                arr = np.asarray(handle.convert("RGB")).astype(int)
            target = rule["value"]
            tolerance = rule.get("tolerance", 18)
            hit = (
                (abs(arr[:, :, 0] - target[0]) < tolerance)
                & (abs(arr[:, :, 1] - target[1]) < tolerance)
                & (abs(arr[:, :, 2] - target[2]) < tolerance)
            ).sum()
            if hit < rule.get("min_pixels", 200):
                print(f"      ↳ 断言失败：颜色 {target} 只命中 {hit} 像素，"
                      f"低于 {rule.get('min_pixels', 200)}")
                return False
        if kind == "color_absent":
            import numpy as np
            from PIL import Image

            with Image.open(images[0]) as handle:
                arr = np.asarray(handle.convert("RGB")).astype(int)
            target = rule["value"]
            tolerance = rule.get("tolerance", 18)
            hit = (
                (abs(arr[:, :, 0] - target[0]) < tolerance)
                & (abs(arr[:, :, 1] - target[1]) < tolerance)
                & (abs(arr[:, :, 2] - target[2]) < tolerance)
            ).sum()
            if hit > rule.get("max_pixels", 200):
                print(f"      ↳ 断言失败：颜色 {target} 命中 {hit} 像素，"
                      f"高于 {rule.get('max_pixels', 200)}")
                return False
        if kind == "height_range":
            from PIL import Image

            with Image.open(images[0]) as handle:
                height = handle.height
            low, high = rule["value"]
            if not low <= height <= high:
                print(f"      ↳ 断言失败：图高 {height} 不在 [{low}, {high}]")
                return False
        if kind == "total_height_range":
            # 分段出图时按总高判定
            from PIL import Image

            total = 0
            for path in images:
                with Image.open(path) as handle:
                    total += handle.height
            low, high = rule["value"]
            if not low <= total <= high:
                print(f"      ↳ 断言失败：切片总高 {total} 不在 [{low}, {high}]")
                return False
        if kind == "text_contains":
            joined = " ".join(texts)
            if rule["value"] not in joined:
                print(f"      ↳ 断言失败：输出 {joined!r} 不含 {rule['value']!r}")
                return False
        if kind == "no_pure_color_block":
            # 只看最长的一条水平连续游程，避免把 emoji / 图标里的同色像素算进来
            import numpy as np
            from PIL import Image

            with Image.open(images[0]) as handle:
                arr = np.asarray(handle.convert("RGB")).astype(int)
            target = rule["value"]
            same = ((abs(arr[:, :, 0] - target[0]) < 12)
                    & (abs(arr[:, :, 1] - target[1]) < 12)
                    & (abs(arr[:, :, 2] - target[2]) < 12))
            longest = 0
            for row in same[::4]:
                xs = np.where(row)[0]
                if not len(xs):
                    continue
                run = best = 1
                for index in range(1, len(xs)):
                    run = run + 1 if xs[index] - xs[index - 1] <= 2 else 1
                    best = max(best, run)
                longest = max(longest, best)
            if longest > rule["max_run"]:
                print(f"      ↳ 断言失败：颜色 {target} 最长连续游程 {longest} "
                      f"> {rule['max_run']}（疑似未被隐藏的色块）")
                return False
    return True


async def run_case(plugin, case, site_dir):
    handler = getattr(plugin, case["handler"])
    text = case["text"].replace("{PORT}", str(SITE_PORT))
    # 真实链路里 AstrBot 已经把命令名剥掉了，GreedyStr 只拿到后面的部分；
    # 夹具要照做，否则 `/渲染截图`（后面什么都不带）会被当成有指令
    parts = text.split(maxsplit=1)
    instruction = parts[1] if len(parts) > 1 else ""
    event = FakeEvent(text)
    started = time.time()
    try:
        async for _ in handler(event, instruction=instruction):
            pass
    except Exception as exc:
        RESULTS.append({"label": case["label"], "cmd": text, "ok": False,
                        "elapsed": round(time.time() - started, 2),
                        "error": f"{type(exc).__name__}: {exc}", "images": [], "texts": []})
        print(f"[FAIL] {case['label']}: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return
    elapsed = time.time() - started
    images = [p for k, p in event.results if k == "image"]
    texts = [p for k, p in event.results if k == "text"]
    for path in list(images):
        if not Path(path).exists():
            raise AssertionError(f"返回的图片路径不存在：{path}")
    expect_image = case.get("expect_image", True)
    ok = bool(images) == expect_image and (bool(images) or bool(texts))
    if ok:
        ok = _check_assertions(case, images, texts)
    RESULTS.append({"label": case["label"], "cmd": text, "ok": ok,
                    "elapsed": round(elapsed, 2), "images": images, "texts": texts})
    print(f"[{'OK' if ok else 'FAIL'}] {case['label']} ({elapsed:.2f}s) "
          f"imgs={len(images)} texts={texts[:1]}")


async def main() -> int:
    cfg = AstrBotConfig()
    ctx = _make_context(cfg)
    pm = PluginManager(ctx, cfg)
    await pm.reload()

    info = pm.context.get_registered_star("astrbot_plugin_screenshot")
    if info is None or info.star_cls is None:
        print("插件未能通过 AstrBot 加载")
        return 2
    print("插件已加载：", info)

    plugin = type(info.star_cls)(pm.context, {"browser_path": os.environ.get("SHOT_BROWSER", "")})

    site_dir = Path(os.environ.get("SHOT_SITE_DIR", "/tmp/shot_site"))
    for source in sorted(HERE.glob("site_*.html")):  # site_index.html → index.html
        (site_dir / source.name[len("site_"):]).write_bytes(source.read_bytes())
    if not (site_dir / "index.html").exists():
        print(f"缺少测试站点：{site_dir}/index.html")
        return 3
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    # 端口被占用时自动往后找：跑完一次不留心清理、或并行跑两遍时，
    # 老实现会直接 OSError: Address already in use，看不出真正原因
    global SITE_PORT
    site_port = SITE_PORT
    httpd = None
    for port in range(site_port, site_port + 20):
        try:
            httpd = ThreadingHTTPServer(
                ("127.0.0.1", port),
                lambda *a, **k: SimpleHTTPRequestHandler(*a, directory=str(site_dir), **k),
            )
            break
        except OSError:
            continue
    if httpd is None:
        print(f"端口 {site_port}~{site_port + 19} 都被占用，无法起测试站点")
        return 3

    SITE_PORT = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"测试站点：http://127.0.0.1:{SITE_PORT}")

    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    try:
        for case in cases:
            await run_case(plugin, case, site_dir)
    finally:
        await plugin.terminate()
        httpd.shutdown()

    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\n=== 结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    for r in failed:
        print("FAIL:", r["label"], r.get("error") or r.get("texts"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
