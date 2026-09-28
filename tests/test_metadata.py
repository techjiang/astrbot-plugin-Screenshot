"""metadata.yaml 自检：面向 AstrBot 官方插件商店的规范校验。

    python tests/test_metadata.py
    python tests/test_metadata.py --require-pillow   # CI 用：缺 Pillow 直接判失败

不依赖 AstrBot 运行环境与网络，纯静态校验 + Pillow 图像校验，
可直接挂进 CI。校验三条线：

1. 身份与字段规范（名称、显示名、版本、作者、仓库、支持版本）
2. 商店记录生成所需的字段是否齐全、取值合法
3. Logo 资源是否真实存在、是否为正方形透明 PNG，以及缩到小尺寸后是否还看得清

**为什么有 `--require-pillow`**：第 7 节那几条渲染侧断言（「缩到 64px 糊不糊」）
只能靠 Pillow 读像素，缺依赖时脚本原本打一行 `[SKIP]` 就退出 0 —— 断言等于白写，
CI 却是绿的。CI 里必须传这个开关，让「依赖没装」变成可见的失败。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []

# CI 传 --require-pillow：缺依赖时不再静默 SKIP，直接判失败
REQUIRE_PILLOW = "--require-pillow" in sys.argv[1:]


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
EXPECTED_NAME = "astrbot_plugin_web_screenshot"
check("name 等于约定的插件目录名", meta.get("name"), EXPECTED_NAME)
if ROOT.name.startswith("astrbot_plugin_"):
    check("name 与所在目录名一致", meta.get("name"), ROOT.name)
ok("name 符合插件命名规范（小写+下划线）",
   bool(re.fullmatch(r"[a-z0-9_]+", meta.get("name", ""))), meta.get("name", ""))

# ---------- 1a) 插件身份：为什么名字不能改回去 ----------
#
# AstrBot Cloud 的插件身份是 `author/name`（author + name），全局唯一，
# 并且按这个身份记账。改名会拿到一个全新的身份，升级版本号不会。
# 老名字 `astrbot_plugin_screenshot` 在平台上已经「查不到、但占着号」：
# 以它为 name 提交只会在登记阶段撞上「该插件已被标记为 deleted、已从公开市场下架」，
# 修 logging、修 Logo、升版本号都救不回来 —— 前几次上架失败就卡在这里。
# 所以这里把新名字钉死：谁「顺手整理命名」改回去，这条会直接失败。
ok("name 不是已被平台标记 deleted 的旧身份 astrbot_plugin_screenshot",
   meta.get("name") != "astrbot_plugin_screenshot",
   "该身份在 AstrBot Cloud 上已下架且被标记 deleted，复用会直接挡在上架登记这一步；"
   "要用新名字（当前 astrbot_plugin_web_screenshot）")

# 插件市场 JSON 规范要求 author / name 非空、去除首尾空白、且不得包含 `/`，
# 平台侧也是靠 `astrbot_plugin_` 前缀把记录认成插件包的。
_name = meta.get("name", "")
ok("name 以 astrbot_plugin_ 开头",
   _name.startswith("astrbot_plugin_"), _name)
ok("name 不含 `/`", "/" not in _name, _name)
ok("author 不含 `/` 且非空",
   bool(meta.get("author", "").strip()) and "/" not in meta.get("author", ""),
   repr(meta.get("author")))

# 包内根目录名就是 metadata.yaml 的 name，装上去之后插件目录也叫这个名字。
# 两处一旦分叉，AstrBot 按目录名加载、平台按 metadata 身份记账，会出现
# 「装上了但更新找不到、或者认成另一个插件」这种最难查的问题。
_zip_name = f'{meta.get("name")}-v{meta.get("version")}.zip'
ok("安装包名遵循 <name>-v<version>.zip 约定",
   bool(re.fullmatch(r"[a-z0-9_]+-v\d+(\.\d+){1,3}\.zip", _zip_name)), _zip_name)

# ---------- 2) 版本号：PEP 440，禁止 v 前缀 ----------

version = meta.get("version", "")
ok("version 不带 `v` 前缀", not version.startswith("v"), version)
ok("version 符合 PEP 440",
   bool(re.fullmatch(r"\d+(\.\d+){1,3}([abrc]\d+|\.post\d+|\.dev\d+)?", version)), version)


def _ver_tuple(text: str) -> tuple[int, ...]:
    """把版本号转成可比较的元组，非数字段（rc/post/dev）按 0 补齐。"""
    parts = re.findall(r"\d+", text)
    return tuple(int(x) for x in parts[:4])


# 版本号只增不减：一个号只能用一次。
# 依据是 AstrBot 官方插件商店的发布约定 —— 「该版本号已经使用过，即使版本已
# 删除或撤回，也不能重复使用」。平台按版本号记账，回退或复用会让「某个版本
# 对应哪份代码」无法追溯。所以这里把「历史最高版本」当作下限来卡：
#   - 下限取 README 徽章 / CHANGELOG 各条目标题里的最大版本号
#   - metadata.yaml 里的 version 必须 >= 该下限
# 一旦有人把版本号写小（典型场景：改完发现要撤回重发，顺手把号降回去），
# 这条会直接失败。
# 注意：CHANGELOG 允许保留比当前版本更高的条目（例如先写了 0.5.2 又顺延到
# 0.5.3），所以只比较「不低于下限」，不强求「等于下限」。
changelog_versions = []
_cl_path = ROOT / "CHANGELOG.md"
if _cl_path.is_file():
    changelog_versions = re.findall(
        r"^##\s*\[?v?(\d+(?:\.\d+){1,3})\]?", _cl_path.read_text(encoding="utf-8"),
        re.M)
readme_versions = re.findall(r"badge/version-v(\d+(?:\.\d+){1,3})",
                             (ROOT / "README.md").read_text(encoding="utf-8"))
seen = [_ver_tuple(v) for v in changelog_versions + readme_versions if _ver_tuple(v)]
floor = max(seen) if seen else ()

if floor:
    def _fmt(t: tuple[int, ...]) -> str:
        return ".".join(str(x) for x in t)

    ok(f"version 不低于历史最高版本 {_fmt(floor)}",
       _ver_tuple(version) >= floor,
       f"当前 {version}；版本号只能用一次、只增不减，"
       f"已删除或撤回的号同样不可复用，请改用更高的新号")
    ok("version 记录在 CHANGELOG 中",
       version in changelog_versions,
       f"CHANGELOG 里没有 {version} 的条目")
    ok("version 与 README 徽章一致",
       (not readme_versions) or version in readme_versions,
       f"README 徽章写的是 {readme_versions}，metadata 是 {version}")

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
    # GitHub 仓库名与插件 name 是两回事，插件市场规范里 repo 明确
    # 「不得用作插件身份」。这个仓库是历史命名（连字符 + 大写 S），
    # 插件 name 下划线小写，两者本来就不会一致，所以这里只校验仓库名合法、
    # 并确认它**没有**被写回插件身份 —— 真正的身份断言在上面的 1a 节。
    ok("repo 仓库名与插件 name 不同源时也能识别（仅校验形态）",
       bool(_repo_name), _repo_name)
    ok("repo 未参与插件身份判定（name 与仓库名解耦）",
       _repo_name != meta.get("name"), f"{_repo_name} vs {meta.get('name')}")

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
    if Image is None and REQUIRE_PILLOW:
        ok("已安装 Pillow（--require-pillow）", False,
           "缺 Pillow，Logo 渲染侧断言无法执行；请先 pip install -r requirements.txt")
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

            # 插件卡片在窄屏下会缩到 32px 上下，这里再兜一层：
            # 主体必须占满画布，不能因为图源留白多而在小尺寸下缩成一小块。
            tiny = im.convert("RGBA").resize((32, 32), Image.LANCZOS)
            tiny_alphas = [a for r, g, b, a in tiny.getdata() if a > 0]
            tiny_ratio = len(tiny_alphas) / (32 * 32)
            ok("Logo 缩到 32px 后主体仍占 > 30% 画布",
               tiny_ratio > 0.30, f"{tiny_ratio:.1%}")
            tiny_faded = sum(1 for a in tiny_alphas if a < 240)
            tiny_faded_ratio = tiny_faded / max(1, len(tiny_alphas))
            ok("Logo 缩到 32px 后没有大面积糊掉（< 45%）",
               tiny_faded_ratio < 0.45, f"半透明占比 {tiny_faded_ratio:.1%}")
    else:
        print("[SKIP] 未安装 Pillow，跳过图像校验")

# ---------- 7a) AstrBot 读取 Logo 的真实约定 ----------
#
# 这一节是「商店/WebUI 里看不到插件 Logo，只显示官方默认图标」的根因拦截。
#
# AstrBot 的 PluginManager 只在**插件目录根**找固定文件名，源码里是：
#
#     self.logo_fname = "logo.png"
#     logo_path = os.path.join(plugin_dir_path, self.logo_fname)
#     if os.path.exists(logo_path):
#         metadata.logo_path = logo_path
#
# 也就是说：`assets/logo.png` 不管放得多整齐都没用，`metadata.yaml` 里的 `logo:`
# 字段也不是 AstrBot 的读取来源（那是提交商店时用的描述字段）。只要根目录没有
# `logo.png`，`metadata.logo_path` 就是 None，WebUI 与商店卡片只能回落到默认图标。
#
# 已经用真 AstrBot 4.14.6 的 PluginManager 实测过：
#   加根目录 logo.png 之前 -> logo_path = None
#   加根目录 logo.png 之后 -> logo_path = .../astrbot_plugin_web_screenshot/logo.png
#
# 所以这里必须硬性要求根目录存在 `logo.png`，且与 `assets/logo.png` 同源同内容，
# 避免以后有人「整理目录」把它挪回 assets/ 里，Logo 又静默消失。

root_logo = ROOT / "logo.png"
ok("插件根目录存在 logo.png（AstrBot 固定读取此路径）", root_logo.is_file(),
   "AstrBot PluginManager 只认 <插件目录>/logo.png；缺少它时 metadata.logo_path 为 None，"
   "WebUI 与商店卡片会回落成官方默认图标")

if root_logo.is_file() and Image is not None:
    with Image.open(root_logo) as rim:
        ok("根目录 logo.png 是正方形", rim.width == rim.height, f"{rim.width}x{rim.height}")
        ok("根目录 logo.png 带 alpha 通道", rim.mode in ("RGBA", "LA"), rim.mode)

# 根目录 logo.png 与 assets/logo.png 必须同源，避免两处各自演化
assets_logo = ROOT / "assets" / "logo.png"
if root_logo.is_file() and assets_logo.is_file():
    ok("根目录 logo.png 与 assets/logo.png 内容一致",
       root_logo.read_bytes() == assets_logo.read_bytes(),
       "两份 Logo 已分叉；请从同一源图重出，避免商店头像与仓库头像不一致")

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
