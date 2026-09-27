# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [v0.5.1] — 2026-09-27

### 修复

- **CI「指令解析回归」Stage 报 `ModuleNotFoundError: No module named “PIL”`**
  （`.cnb.yml`）：`tests/test_parse.py` 虽然只测纯函数，但经 `core.image` /
  `core.session` **间接 import PIL**，构建镜像里没装 `requirements.txt`，import
  阶段就炸，后面两个 Stage 连带被跳过。现在该 Stage 先装依赖再跑检查。
- **默认构建镜像里没有 Python**：脚本写 `python ...`，但镜像里既没有 `python`
  也没有 `python3`（`sh: python3: command not found`，127），`语法检查` Stage 直接红。
  现在 Pipeline 级固定 `python:3.11-slim-bookworm` 镜像，脚本统一用 `python3`，
  不再依赖构建机默认 PATH。
- **镜像要选 `bookworm` 变体**：`python:3.11-slim`（trixie 底）要 `GLIBC_2.38`，
  跟 runner 宿主对不上，Stage 瞬间 error、且错误信息里看不到 Python 本身的报错。
  `python:3.11-slim-bookworm` 可正常执行。
- **`tests/test_metadata.py` 的图像断言会被静默跳过**：该文件在缺 Pillow 时打
  `[SKIP] 未安装 Pillow` 后仍以 0 退出，等于新增的「Logo 缩到 64px 糊不糊」
  断言白写。新增 `--require-pillow` 开关，CI 一律带上，缺依赖即 FAIL。
- **CI 的 `A && B` 写法有假绿风险**：CNB 的 `script` 每段新建 shell，`pip install`
  作最后一条命令时以 0 收尾；若某镜像里 pip 缺失（打印 127 仍退出 0，CNB 不按 127
  判错），`&&` 后的检查会被整个跳过、Stage 变绿。改为多行，让检查一定执行，由检查自己决定 Stage 成败。
- **商店头像缩到小尺寸后糊成一片**：`assets/logo.png` 直接用了作者提供的整图
  （图标 + `Screenshot` + `AstrBot Plugin` 两行标题）缩到 256×256，商店列表与
  插件卡片里只有 40px 上下，标题文字退化成一片灰雾，看起来像图没渲染好。
  现在头像**只取图标本体**，标题交给 `assets/logo-banner.png` 横幅。
- **`assets/logo-banner.png` 是正方形**：README 顶部按 `width=320` 展示，正方形图
  带着大片空白，视觉上偏小；改为 1280×720 的透明横幅。
- `assets/logo-128.png` / `logo-512.png` 与 `logo.png` 不再是同一张图的缩放
  （此前 128 与 512 是另一次渲染，细节与 256 对不上）。

### 新增

- `tests/test_metadata.py` 增加 3 条 Logo 渲染侧断言：头像必须**只命中图标本体**
  （缩到 64px 后半透明「糊掉区域」< 25%、可见主体 > 15% 画布），横幅必须存在、
  带 alpha、四角透明。反向验证过：拿旧头像跑必然 FAIL（半透明占比 35.5%）。
- `tests/test_metadata.py` 新增 `--require-pillow` 开关：缺 Pillow 时不再静默 SKIP，
  直接判失败；CI 一律带上这个开关。

## [v0.5.0] — 2026-09-27

### 修复

- **显式视口的出图多出一倍像素**：`/截图 x.com viewport 1280x800` 出图是
  2560x1600 而不是 1280x800。根因是 `Page.captureScreenshot` 在设置了 device
  metrics override 时返回 `视口 CSS 尺寸 × deviceScaleFactor` 的位图，而显式视口
  内部统一按 DPR 2 描述，等于被放大两次。现在 `viewport` 模式会把位图按同一个
  DPR 缩回 CSS 尺寸，「输出像素 = 你指定的视口像素」。（`iphone viewport` 这类
  移动端隐藏多出的像素是既有行为，本次一并把用例期望值对齐为 390x844）
- `Page.setDocumentContent` 之后补发的 metrics 改为「原样重设一份参数」，
  不再走 `_apply_metrics`（后者会额外触发一次透明背景重设），渲染 HTML 更稳定

### 新增

- 4 条像素级回归：显式视口输出尺寸、`scale=0.5` 不缩放、手机预设 × 显式视口、
  「先分段长页再截可视区」不复用被拉高的视口

## [v0.4.0]


### 新增

- **透明背景**：`transparent` 参数与配置项，出真正的 RGBA PNG（配合
  `Emulation.setDefaultBackgroundColorOverride`），适合截 Logo、图标、无背景组件
- **元素截图留白**：`padding=` 参数与配置项，向外扩出 `box-shadow` / `outline` /
  圆角光晕，不再被 border box 切掉
- **渲染观察窗口**：`render_watch_ms` 配置项，用 MutationObserver 兜住
  `setTimeout` 后才挂图 / 改版的页面
- **PDF 分页重写**：按段分页，正文不再被从中间横切
- **`print` 媒体**：切到页面自带的 `@media print` 样式，方便截「干净正文」
- 新的 e2e 用例（共 40 条），新增 `site_alpha.html` 与延迟挂载图片夹具

### 修复

- 整页长图中元素截图的水印整块消失（水印与裁剪现在共用同一个矩形）
- 元素截图整圈切掉阴影、描边与圆角光晕（改用视觉四边形并集）
- 无背景页面的空白处恒为白色（两层配合才拿到 alpha 通道）
- 延迟挂载的图片被截成「加载中」占位图
- 长图最后一片只剩几十像素导致水印看起来「跑到画面中部」（切片改为均分）
- `max_height=0`（本次不切片）被当成「没填」
- 引号内转义引号被切成碎片，多余 token 还会污染选择器

### 文档

- 重写 `README.md`：定位、安装、功能、用法、参数表、配置、行为说明、测试入口
- 新增 `docs/USAGE.md`：参数全表、场景示例、解析规则、报错对照表、出图排查
- 新增 `docs/DEVELOPMENT.md`：架构、CDP 调用清单、踩坑记录、测试与发布流程
- 新增 `docs/FAQ.md` 与 `CHANGELOG.md`
- 新增 `tests/test_docs.py`：文档一致性回归（配置键 / 参数 key / 帮助文本三向对齐），
  并接入 PR 流水线
- `main.HELP_TEXT` 补上 `/截图帮助`

## [v0.3.0]

### 新增

- **多格式输出**：`format=png|jpeg|pdf`、`quality=`，超长文档可直接出 PDF
- **并发闸门**：`max_concurrent` 配置，超出自动排队
- **代理与请求头**：`proxy`（支持 socks5 与账号密码）、`headers` 配置
- **大图降 DPR**：`max_dpr` 配置，整页本来就要分段时自动降一档
- **等待三级保险**：load 事件 → 网络静默 → 图片解码完成
- 截到纯色空白图自动退避重试
- 设备中文别名（`手机` `平板` `电脑`）与 `file://` / 绝对路径支持
- 项目 Logo 入库

### 修复

- 整页长图把 `position:fixed` 元素渲染到文档底（视口拉高 + 分段滚动对齐）
- 浏览器崩溃后整批请求全灭（自动重启并重试）
- `Page.navigate` 传 `timeout` 会污染渲染上下文（页面按旧视口布局）
- `selector=#a > .b` 被空格切断
- 越界参数被静默接受（现在统一夹取）
- 空白图判定阈值算错，重试机制形同虚设
- 缓存目录只增不减
- `_persist` 硬编码扩展名，新格式下文件头与后缀不符

## [v0.2.0]

### 新增

- `assets/logo.png` 与 `assets/logo-256.png`，README 顶部展示
- 元数据补齐作者信息

### 修复

- 同会话连续截图互相覆盖（文件名加时间戳）
- 缓存清理删掉本轮刚写出的产物
- `viewport_for` 未对设备名做 `strip/lower`

## [v0.1.0]

### 新增

- 首个版本：CDP 直驱的截图插件
- 整页 / 可视区 / 元素 / HTML 渲染四类截图
- 设备预设、`scale=`、`dark`、`hide=`、`watermark=`、`wait=`、`waitms=`、`timeout=`
- 长图自动切片与体积兜底
- Chromium 进程常驻、标签页用完即关
