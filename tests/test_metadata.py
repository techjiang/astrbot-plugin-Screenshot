"""metadata.yaml 自检：面向 AstrBot 官方插件商店的规范校验。

    python tests/test_metadata.py

不依赖 AstrBot 运行环境与网络，纯静态校验 + 可选的 Pillow 图像校验，
可直接挂进 CI。校验三条线：

1. 身份与字段规范（名称、显示名、版本、作者、仓库、支持版本）
2. 商店记录生成所需的字段是否齐全、取值合法
3. Logo 资源是否真实存在、是否为正方形透明 PNG
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: 期望 {expected!r}，实际 {actual!r}")
        print(f"[FAIL] {label}: 期望 {expected!r}，实际 {actual!r}")
    else:
        print(f"[OK] {label}")


def ok(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        FAILURES.append(f"{label}{(' — ' + detail) if detail else ''}")
        print(f"[FAIL] {label}{(' — ' + detail) if detail else ''}")
    else:
        print(f"[OK] {label}")


# ---------- 极简 YAML 读取（只覆盖本项目 metadata.yaml 用到的语法） ----------

def load_metadata(path: Path) -> dict:
    data: dict = {}
    key = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent == 0 and ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value in ("|", ">"):
                data[key] = ""
            elif value == "":
                data[key] = []
            else:
                data[key] = _unquote(value)
        elif line.startswith("- ") and isinstance(data.get(key), list):
            data[key].append(_unquote(line[2:].strip()))
        elif isinstance(data.get(key), str):
            data[key] = (data[key] + "\n" + line).strip()
    return data


def _unquote(value: str):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


meta = load_metadata(ROOT / "metadata.yaml")

# ---------- 1) 身份与必填字段 ----------

REQUIRED = ["name", "display_name", "desc", "version", "author", "repo", "astrbot_version"]
for field in REQUIRED:
    ok(f"必填字段 `{field}` 存在", bool(meta.get(field)), "缺失或为空")

# 目录名：本地开发时根目录可能是 workspace/仓库名，不强制等于 name；
# 但若根目录本身形如 astrbot_plugin_xxx，则必须与 name 一致。
EXPECTED_NAME = "astrbot_plugin_screenshot"
check("name 等于约定的插件目录名", meta.get("name"), EXPECTED_NAME)
if ROOT.name.startswith("astrbot_plugin_"):
    check("name 与所在目录名一致", meta.get("name"), ROOT.name)
ok("name 符合插件命名规范（小写+下划线）",
   bool(re.fullmatch(r"[a-z0-9_]+", meta.get("name", ""))), meta.get("name", ""))

# ---------- 2) 版本号：PEP 440，禁止 v 前缀 ----------

version = meta.get("version", "")
ok("version 不带 `v` 前缀", not version.startswith("v"), version)
ok("version 符合 PEP 440",
   bool(re.fullmatch(r"\d+(\.\d+){1,3}([abrc]\d+|\.post\d+|\.dev\d+)?", version)), version)

# ---------- 3) 仓库地址：商店要求可公开访问的 GitHub 仓库 ----------

repo = meta.get("repo", "")
ok("repo 指向 GitHub 仓库",
   bool(re.match(r"^https://github\.com/[\w.-]+/[\w.-]+/?$", repo)), repo)
# 商店会把 repo 当成安装源直接用，指向不存在的仓库等于装不上。
# 这条只能做静态的形状校验：仓库是否真的存在需要联网，不适合放进零依赖自检。
# 线上踩过一次（下划线 vs 连字符写法不同、链接 404），所以这里至少拦住
# 「占位符 / 本地地址」这类明显写错，并把「下划线⇄连字符、大小写」归一后再比一次。
ok("repo 不是本地占位/示例地址",
   not re.search(r"(your[-_]?name|example\.com|localhost|127\.0\.0\.1|<[^>]+>)", repo, re.I), repo)
_owner_repo = re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+?)/?", repo or "")
ok("repo 拆得出 owner/repo", bool(_owner_repo), repo)
if _owner_repo:
    _owner, _repo_name = _owner_repo.groups()
    # 仓库名合法字符：字母数字、点、下划线、连字符，且不能以点开头/结尾
    ok("repo 仓库名合法",
       bool(re.fullmatch(r"(?![.])[\w.-]+(?<![.])", _repo_name)), _repo_name)
    # GitHub 习惯用连字符（astrbot-plugin-Screenshot），插件目录约定用下划线
    # （astrbot_plugin_screenshot），两者归一后应当同源。
    ok("repo 仓库名与插件名同源（下划线⇄连字符、大小写不计）",
       _repo_name.lower().replace("-", "_") == EXPECTED_NAME,
       f"{_repo_name} vs {EXPECTED_NAME}")

# ---------- 4) AstrBot 版本约束 ----------

spec = meta.get("astrbot_version", "")
ok("astrbot_version 是版本约束表达式",
   bool(re.match(r"^[><=!~^]*\s*\d+(\.\d+)*", spec)), spec)

# ---------- 5) 新版商店字段 ----------

ok("short_desc 存在且不超过 60 字",
   bool(meta.get("short_desc")) and len(meta["short_desc"]) <= 60,
   f"len={len(meta.get('short_desc', ''))}")

platforms = meta.get("support_platforms") or []
ok("support_platforms 非空且为列表", isinstance(platforms, list) and len(platforms) > 0)

VALID_PLATFORMS = {
    "aiocqhttp", "qq_official", "telegram", "discord", "slack", "lark",
    "dingtalk", "wecom", "kook", "vocechat", "misskey", "satori", "webchat",
}
unknown = [p for p in platforms if p not in VALID_PLATFORMS]
ok("support_platforms 取值合法", not unknown, f"未知平台: {unknown}")

tags = meta.get("tags") or []
ok("tags 非空且不超过 6 个", isinstance(tags, list) and 0 < len(tags) <= 6, f"{tags}")

# ---------- 6) 保留字段不应被误用 ----------

ok("未使用保留字段 `avatar`（Logo 由 logo 字段指定）", "avatar" not in meta)
ok("未使用保留字段 `logo_url`", "logo_url" not in meta)

# ---------- 7) Logo 资源 ----------

logo_rel = meta.get("logo", "assets/logo.png")
logo_path = ROOT / logo_rel
ok(f"Logo 文件存在（{logo_rel}）", logo_path.is_file())

if logo_path.is_file():
    try:
        from PIL import Image
    except ImportError:
        Image = None
    if Image is not None:
        with Image.open(logo_path) as im:
            ok("Logo 是正方形", im.width == im.height, f"{im.width}x{im.height}")
            ok("Logo 宽度为商店推荐 256", im.width == 256, str(im.width))
            ok("Logo 带 alpha 通道", im.mode in ("RGBA", "LA"), im.mode)
            if im.mode in ("RGBA", "LA"):
                alpha = im.convert("RGBA").getchannel("A")
                lo, hi = alpha.getextrema()
                ok("Logo 背景透明（存在 alpha=0 像素）", lo == 0, f"alpha min={lo}")
                ok("Logo 有可见内容（存在 alpha=255 像素）", hi == 255, f"alpha max={hi}")
                px = im.convert("RGBA").load()
                corners = [px[0, 0], px[im.width - 1, 0], px[0, im.height - 1], px[im.width - 1, im.height - 1]]
                ok("Logo 四角全透明", all(c[3] == 0 for c in corners), str(corners))

            # 商店列表、插件卡片里会把这个文件缩到 40px 上下显示，
            # 「能打开」不等于「看得清」。这类问题只能从渲染侧反推：
            # 缩图里「已经糊掉的区域」占比 = 半透明像素的比例。
            # 正常图标缩下去是实心色块或干净镂空；塞了小字 / 细线的图，
            # 细笔画被均值稀释成大片半透明灰，看着就像渲染坏了。
            small = im.convert("RGBA").resize((64, 64), Image.LANCZOS)
            alphas = [a for r, g, b, a in small.getdata() if a > 0]
            faded = sum(1 for a in alphas if a < 240)
            ratio = faded / max(1, len(alphas))
            ok("Logo 缩到 64px 后主体仍然实心（糊掉区域 < 25%）",
               ratio < 0.25, f"半透明占比 {ratio:.1%}")
            ok("Logo 缩到 64px 后仍有可见主体（> 15% 画布）",
               len(alphas) / (64 * 64) > 0.15, f"{len(alphas) / (64 * 64):.1%}")
    else:
        print("[SKIP] 未安装 Pillow，跳过图像校验")

# ---------- 7b) 横幅图 ----------

banner = ROOT / "assets" / "logo-banner.png"
ok("横幅图 assets/logo-banner.png 存在", banner.is_file())
if banner.is_file() and Image is not None:
    with Image.open(banner) as bim:
        ok("横幅图带 alpha 通道", bim.mode in ("RGBA", "LA"), bim.mode)
        ok("横幅图四角透明",
           all(bim.convert("RGBA").getpixel(pt)[3] == 0
               for pt in ((0, 0), (bim.width - 1, 0), (0, bim.height - 1),
                          (bim.width - 1, bim.height - 1))))

# ---------- 8) desc 内容 ----------

desc = meta.get("desc", "")
ok("desc 有实质内容（>30 字）", len(desc) > 30, f"len={len(desc)}")
ok("desc 覆盖核心能力（截图/整页/CDP 至少两项）",
   sum(k in desc for k in ("截图", "整页", "CDP")) >= 2)

# ---------- 结果 ----------

print()
if FAILURES:
    print(f"=== 失败 {len(FAILURES)} 项 ===")
    for item in FAILURES:
        print(" -", item)
    raise SystemExit(1)
print("=== metadata 自检全部通过 ===")
