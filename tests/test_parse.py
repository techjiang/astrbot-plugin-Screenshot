"""指令解析回归：不依赖 AstrBot / 浏览器 / Pillow，可直接在 CI 里跑。

    python tests/test_parse.py

端到端实测（tests/e2e_astrbot.py）需要真 Chromium 与 AstrBot 运行环境，
不适合放在每次 PR 的流水线里；但参数解析是纯函数，改动频率又最高，
所以单独抽出来做成零依赖回归。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import (  # noqa: E402
    as_float,
    as_int,
    load_extra_headers,
    parse_bool,
    parse_instruction,
    suggest_url,
    viewport_for,
)
from core.image import detect_mime, normalise_scale, suggest_suffix  # noqa: E402

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: 期望 {expected!r}，实际 {actual!r}")
        print(f"[FAIL] {label}: 期望 {expected!r}，实际 {actual!r}")
    else:
        print(f"[OK] {label}")


def shot(text: str, **defaults):
    return parse_instruction(text, defaults=defaults or None)


# ---------- 基础模式 ----------

check("默认整页", shot("example.com").mode, "full")
check("viewport 模式", shot("example.com viewport").mode, "viewport")
check("full 关键字", shot("example.com full").mode, "full")
check("整页中文别名", shot("example.com 整页").mode, "full")
check("首屏中文别名", shot("example.com 首屏").mode, "viewport")
check("裸选择器 → 元素模式", shot("example.com #main").mode, "element")
check("selector= → 元素模式", shot("example.com selector=#main").selector, "#main")
check("el= 别名", shot("example.com el=.card").selector, ".card")
check("html: 前缀 → 渲染", shot("html:<b>x</b>").mode, "render")
check("渲染关键字", shot("example.com 渲染").mode, "render")

# ---------- 设备与缩放 ----------

check("iphone 预设", shot("example.com iphone").device, "iphone")
check("手机中文别名", shot("example.com 手机").device, "iphone")
check("pc 别名", shot("example.com pc").device, "desktop")
check("平板别名", shot("example.com 平板").device, "pad")
check("直接写视口", shot("example.com 1440x900").device, "1440x900")
check("scale=", shot("example.com scale=2").scale, 2.0)
check("2x 写法", shot("example.com 2x").scale, 2.0)
check("scale 越界夹取", shot("example.com scale=99").scale, 4.0)
# scale 下界按比例夹到 0.2（图像学上仍可辨），不是回落到默认 1
check("scale 下界夹取", shot("example.com scale=0").scale, 0.2)

check("iphone 视口", viewport_for("iphone")[0], (390, 844))
check("iphone DPR", viewport_for("iphone")[1], 3)
check("iphone 移动端", viewport_for("iphone")[2], True)
check("未知设备回落 desktop", viewport_for("nope")[0], (1600, 1000))

# ---------- 参数 ----------

check("dark 关键字", shot("example.com dark").dark, True)
check("dark=false", shot("example.com dark=false").dark, False)
check("light 覆盖 dark", shot("example.com dark light").dark, False)
check("wait=", shot("example.com wait=.loaded").wait_for, ".loaded")
check("waitms=", shot("example.com waitms=800").wait_ms, 800)
check("waitms 越界夹取", shot("example.com waitms=-5").wait_ms, 0)
check("hide= 单值", shot("example.com hide=.ad").hide, [".ad"])
check("hide= 多值", shot("example.com hide=.ad,.float").hide, [".ad", ".float"])
check("hide= 重复累加", shot("example.com hide=.a hide=.b").hide, [".a", ".b"])
check("watermark=", shot("example.com watermark=@bot").watermark, "@bot")
check("watermark 含中文", shot("example.com watermark=科技酱").watermark, "科技酱")
check("timeout 越界夹取", shot("example.com timeout=1").timeout_ms, 1000)
check("timeout 上界夹取", shot("example.com timeout=99999999").timeout_ms, 600000)
check("format=jpg → jpeg", shot("example.com format=jpg").img_format, "jpeg")
check("format=pdf", shot("example.com format=pdf").img_format, "pdf")
check("裸 pdf 关键字", shot("example.com pdf").img_format, "pdf")
check("未知格式回落 png", shot("example.com format=bmp").img_format, "png")
check("quality=", shot("example.com quality=60").quality, 60)
check("max_height=", shot("example.com max_height=12000").max_height, 12000)
check("print 关键字", shot("example.com print").print_media, True)
check("print=false", shot("example.com print=false").print_media, False)

# ---------- 选择器与引号 ----------

check("含空格选择器（引号）",
      shot('example.com "#main > .card"').selector, "#main > .card")
# key= 的裸值遇到空格仍会被切开（与 shell 一致）：这类选择器请加引号
check("含空格选择器（key= 带引号）",
      shot('example.com selector="#main > .card"').selector, "#main > .card")
check("含空格选择器（key= 裸值会被空格切开）",
      shot("example.com selector=#main > .card").selector, "#main")
check("带引号的 key=",
      shot('example.com selector="#a .b"').selector, "#a .b")

# ---------- 顺序无关 ----------

a = shot("example.com iphone scale=2 dark")
b = shot("dark scale=2 iphone example.com")
check("顺序无关：模式", (a.mode, a.device, a.scale, a.dark), (b.mode, b.device, b.scale, b.dark))

# ---------- 配置默认值 ----------

check("配置默认设备", shot("example.com", device="laptop").device, "laptop")
check("配置默认 format", shot("example.com", image_format="jpeg").img_format, "jpeg")
check("命令覆盖配置", shot("example.com device=pad", device="laptop").device, "pad")
check("配置默认 dark", shot("example.com", dark="true").dark, True)

# ---------- URL 归一 ----------

check("裸域名补 https", suggest_url("example.com"), "https://example.com")
check("localhost 用 http", suggest_url("localhost:8080"), "http://localhost:8080")
check("127.0.0.1 用 http", suggest_url("127.0.0.1:9000/x"), "http://127.0.0.1:9000/x")
check("https 原样", suggest_url("https://a.com/b"), "https://a.com/b")
check("file:// 原样", suggest_url("file:///tmp/a.html"), "file:///tmp/a.html")
check("绝对路径原样", suggest_url("/tmp/a.html"), "/tmp/a.html")
check("相对路径按 base 补全",
      suggest_url("sub.html", "https://a.com/dir/page.html"), "https://a.com/dir/sub.html")

# ---------- 工具函数 ----------

check("parse_bool 中文开", parse_bool("开"), True)
check("parse_bool 中文否", parse_bool("否"), False)
check("parse_bool 缺省", parse_bool(None, True), True)
check("parse_bool 乱值回落", parse_bool("maybe", True), True)
check("as_int 非法回落", as_int("abc", 7), 7)
check("as_int 小数截断", as_int("3.9", 0), 3)
check("as_float NaN 回落", as_float("nan", 1.5), 1.5)
check("as_float 夹取", as_float("9", 1.0, high=4.0), 4.0)
check("headers 文本解析",
      load_extra_headers({"headers": "Cookie: a=b\nX-T: 1"}), {"Cookie": "a=b", "X-T": "1"})
check("headers dict 解析", load_extra_headers({"headers": {"A": "1"}}), {"A": "1"})
check("headers 缺省", load_extra_headers({}), {})

check("normalise_scale 上界", normalise_scale(99), 4.0)
check("normalise_scale 非法", normalise_scale("x"), 1.0)
check("detect_mime png", detect_mime(b"\x89PNG\r\n\x1a\n123"), "image/png")
check("detect_mime jpeg", detect_mime(b"\xff\xd8\xff\xe0"), "image/jpeg")
check("detect_mime pdf", detect_mime(b"%PDF-1.7"), "application/pdf")
check("suggest_suffix pdf", suggest_suffix(b"%PDF-1.4"), ".pdf")
check("suggest_suffix 未知", suggest_suffix(b"zzz"), ".bin")

# ---------- 异常输入 ----------

check("空指令不炸", shot("").mode, "full")
check("只有空格不炸", shot("   ").mode, "full")
check("超长参数串不炸", len(shot("example.com " + " ".join(f"hide=.c{i}" for i in range(200))).hide), 200)
check("不配对的引号也能解析", shot('example.com "#main').selector != "", True)

print()
if FAILURES:
    print(f"=== 失败 {len(FAILURES)} 项 ===")
    for item in FAILURES:
        print(" -", item)
    raise SystemExit(1)
print("=== 指令解析回归全部通过 ===")
