"""文档一致性回归：把文档里承诺的参数 / 配置键与代码对齐。

    python tests/test_docs.py

纯静态检查，零依赖，放进 CI 的 PR 阶段很便宜。检查三件事：

1. ``_conf_schema.json`` 的每个配置键都在 README 的配置表里出现；
2. ``main.py`` 读的配置键都真实存在于 schema；
3. ``core/config.py`` 里认识的指令参数 key 都在使用文档里有说明。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []

# HELP_TEXT 定义在 main.py 末尾，而 main.py 顶部 import astrbot；
# 这里只在源码层面抠出来，避免为了跑文档检查而要求装 AstrBot。
_HELP = re.search(r'HELP_TEXT = """(.*?)"""', (ROOT / "main.py").read_text(encoding="utf-8"),
                  re.S)
HELP_TEXT = _HELP.group(1) if _HELP else ""

# 参数 key 的权威清单：直接来自 _apply_kv 的判定分支，避免文档落后于代码
KV_KEYS = (
    "scale", "dpr", "zoom", "device", "viewport", "print", "打印", "media",
    "mobile", "h5", "dark", "theme", "full", "fullpage", "full_page",
    "selector", "el", "element", "选择器", "wait", "wait_for", "waitms", "delay",
    "timeout", "timeout_ms", "hide", "remove", "watermark", "mark",
    "format", "图片格式", "quality", "q", "质量",
    "max_height", "maxheight", "切片高度", "padding", "pad", "留白", "边距",
    "transparent", "透明", "alpha", "bg",
)
# 主循环里的关键字模式（不带 = 的开关）
FLAG_WORDS = (
    "full", "fullpage", "整页", "长图", "viewport", "screen", "首屏", "可视区",
    "render", "html", "渲染", "dark", "夜间", "暗色", "light", "亮色", "浅色",
    "print", "打印", "打印样式", "pdf", "transparent", "透明", "透明背景", "alpha",
)


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"[OK] {label}")
    else:
        FAILURES.append(f"{label}：{detail}")
        print(f"[FAIL] {label}：{detail}")


def main() -> int:
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    usage = (ROOT / "docs" / "USAGE.md").read_text(encoding="utf-8")
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")

    for key, spec in schema.items():
        check(f"README 收录配置 {key}", f"`{key}`" in readme)
        check(f"schema 声明 {key} 的默认值", "default" in spec)
        check(f"schema 说明 {key}", bool(str(spec.get("description", "")).strip()))

    used = set(re.findall(r'_conf\("([^"]+)"', main_src))
    used |= set(re.findall(r'defaults\.get\("([^"]+)"\)', main_src))
    unknown = sorted(k for k in used if k not in schema and not k.startswith("extra_"))
    check("main.py 读到的配置键都在 schema 里", not unknown, str(unknown))

    for key in KV_KEYS:
        check(f"使用文档说明参数 {key}=", f"{key}=" in usage or f"`{key}`" in usage)
    for word in FLAG_WORDS:
        check(f"使用文档说明开关 {word}", word in usage)

    for token in ("/截图", "/元素截图", "/渲染截图", "/截图帮助"):
        check(f"帮助文本包含 {token}", token in HELP_TEXT)
        check(f"README 包含 {token}", token in readme)

    check("README 指向使用文档", "docs/USAGE.md" in readme)
    check("README 指向开发文档", "docs/DEVELOPMENT.md" in readme)
    check("README 指向 FAQ", "docs/FAQ.md" in readme)

    # 文档里的图片/相对链接必须真实存在。
    # 线上踩过一次：README 里引用的仓库内相对路径写错、以及外链在平台上直接 404，
    # 结果「Logo 不显示」被当成插件问题排查了半天。仓库内的引用必须能静态验证。
    for doc in (readme, usage, (ROOT / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8"),
                (ROOT / "docs" / "FAQ.md").read_text(encoding="utf-8"),
                (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")):
        for ref in sorted(set(re.findall(r'(?:src|href)="((?!https?://|#|mailto:)[^"]+)"', doc))):
            target = ref.split("#", 1)[0]
            if not target:
                continue
            check(f"文档引用的仓库内路径存在 {ref}", (ROOT / target).exists())

    # 日志写法必须与商店规范一致：logger 只能来自 astrbot.api。
    # 线上踩过一次：代码已改成 `from astrbot.api import logger`，但文档里还留着
    # 旧的自建命名空间 `astrbot.screenshot`，会把人带回违规写法（v0.5.1 就是被
    # 商店的 LLM Guard 以「禁止使用内置 logging」驳回的）。
    docs_all = "\n".join(
        (ROOT / rel).read_text(encoding="utf-8")
        for rel in ("README.md", "CHANGELOG.md", "docs/USAGE.md", "docs/DEVELOPMENT.md",
                    "docs/FAQ.md")
    )
    for module, src in (("main.py", main_src),
                        ("core/browser.py", (ROOT / "core" / "browser.py").read_text(encoding="utf-8")),
                        ("core/session.py", (ROOT / "core" / "session.py").read_text(encoding="utf-8"))):
        check(f"{module} 不使用内置 logging", not re.search(r'^\s*(?:import logging|from logging import)', src, re.M))
        check(f"{module} 从 astrbot.api 取 logger", "from astrbot.api import" in src and "logger" in src)
    check("文档不再提旧命名空间 astrbot.screenshot",
          "`astrbot.screenshot`" not in docs_all,
          "文档里出现 `astrbot.screenshot`，那是内置 logging 时代的写法")

    if FAILURES:
        print(f"\n=== 文档检查：{len(FAILURES)} 项不一致 ===")
        for item in FAILURES:
            print("FAIL:", item)
        return 1
    print("\n=== 文档检查全部通过 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
