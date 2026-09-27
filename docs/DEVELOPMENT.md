# Screenshot 开发文档

面向改代码的人：架构、CDP 调用链、为什么这么写、怎么测、怎么发版。

- [1. 设计约定](#1-设计约定)
- [2. 模块划分](#2-模块划分)
- [3. 一次截图的生命周期](#3-一次截图的生命周期)
- [4. CDP 调用清单](#4-cdp-调用清单)
- [5. 关键实现与踩坑记录](#5-关键实现与踩坑记录)
- [6. 参数解析器](#6-参数解析器)
- [7. 测试](#7-测试)
- [8. 调试技巧](#8-调试技巧)
- [9. 发布流程](#9-发布流程)
- [10. 贡献约定](#10-贡献约定)

---

## 1. 设计约定

1. **只用 CDP。** 不引入 Playwright / Selenium / pyppeteer / html2image。
   所有浏览器操作都通过 WebSocket 发 CDP 原生命令，只依赖 `aiohttp` + `Pillow`。
   好处是没有驱动版本漂移，坏处是所有便利 API 都得自己写一层薄封装。
2. **浏览器进程常驻。** 插件加载后只拉一次 Chromium，每次截图开一个标签页、用完即关。
   不要为了一次截图反复起停进程（冷启动 1–2 秒）。
3. **不猜，先复现。** 任何「渲染结果不对」的问题，先写最小页面 + 真实浏览器复现，
   定位到 CDP 层面的确切原因再改。本文件第 5 节的每条结论都是实测得来的。
4. **失败要可读。** 所有可预期失败都转成中文一句话提示，不允许静默无响应或裸抛异常。
5. **尽力而为，不影响主流程。** 缓存清理、隐藏元素、释放对象之类的收尾动作
   全部 `suppress` 掉异常。

## 2. 模块划分

```
main.py              Star 子类：指令注册、配置读取、产物落盘与缓存清理
core/browser.py      BrowserProcess（进程/端口/代理）+ CDPConnection（请求响应与事件分发）
core/config.py       指令串分词、参数归一、设备预设、URL 归一
core/image.py        切片（plan_tiles）、编码（PNG/JPEG）、PDF 分页、HTML 骨架
core/session.py      ScreenshotSession：导航、等渲染、四类截图、分段拼接
tests/              e2e_astrbot.py（真 AstrBot + 真 Chromium）、test_parse.py、cases.json、站点夹具
```

依赖方向是单向的：`main → session → {browser, config, image}`，
`config` 与 `image` 不反向依赖 `session`，因此这两块可以纯函数式单测。

## 3. 一次截图的生命周期

以 `/截图 example.com #main watermark=@bot` 为例：

```
AstrBot 消息
  └─ filter.command("截图") → ScreenshotPlugin.screenshot(GreedyStr)
      └─ _build_options()          core/config.parse_instruction 解析指令串
          └─ _capture(opts)        取（或创建）常驻 ScreenshotSession
              └─ ScreenshotSession.capture(opts)
                  └─ _with_page()  并发闸门 → 开标签页 → 干活 → 关标签页（失败重启重试）
                      └─ _render(conn, opts)
                          1. _prepare          Page.enable / Runtime.enable / 额外请求头
                          2. _apply_metrics    设备视口 + DPR + 媒体查询 + 透明背景
                          3. _navigate         Page.navigate → load 事件 → 网络静默 → 字体就绪
                          4. wait=/waitms=     等元素 / 固定延迟
                          5. _wait_images      MutationObserver 观察窗口 + 等图片解码
                          6. _apply_hide       隐藏干扰元素
                          7. _element_rect     元素模式下算裁剪矩形（水印与裁剪共用）
                          8. _apply_watermark  按 整页/元素/可视区 三种定位策略贴水印
                          9. _capture_with_retry → _capture_by_mode
                             元素 → _capture_element（clip = 视觉盒）
                             整页 → _capture_full_page（超限则 _shoot_strips 拼接）
                             可视区 → captureScreenshot(captureBeyondViewport=False)
      └─ core/image.to_bytes()      按需切片 / 转 JPEG / 转 PDF
      └─ _persist()                 写盘 + 清理旧缓存（保护本次产物）
      └─ event.image_result()       逐张发出
```

**并发模型**：一个 `asyncio.Lock` 保护进程启停，一个 `asyncio.Semaphore(max_concurrent)`
限制同时在跑的标签页数。CDP 连接按标签页独立（各自的 WebSocket），互不干扰。

**重试分两层**：
- `_with_page`：捕获 `CDPError` / `aiohttp.ClientError`，重启整台浏览器后重试（最多 2 次）
- `_capture_with_retry`：捕获「截到纯色空白图」，指数退避重试（最多 3 次）

## 4. CDP 调用清单

想改行为时，按这张表定位该动哪条命令。

| 域与命令 | 用在哪 | 备注 |
| --- | --- | --- |
| `Page.enable` / `Runtime.enable` | 每个新标签页开头 | `Runtime` 不 enable 拿不到 objectId |
| `Network.enable` + `Network.setExtraHTTPHeaders` | 导航前 | 自定义请求头由配置 `headers` 提供 |
| `Emulation.setDeviceMetricsOverride` | 设视口 / DPR / mobile | 也用于整页前把视口拉高、分段时逐条设高 |
| `Emulation.setEmulatedMedia` | 暗色 / 打印媒体 | `features=[prefers-color-scheme]`，`media="print"` 可选 |
| `Emulation.setDefaultBackgroundColorOverride` | 透明背景 | 传 `{a:0}` 开，再次调用不带参数关 |
| `Emulation.setScrollbarsHidden` | 分段截取期间 | 避免拼接时每段都带滚动条 |
| `Page.navigate` | 打开网址 | 自身超时固定 60s，与用户的 `timeout=` 解耦 |
| `Page.loadEventFired`（事件） | 等页面加载 | 拿不到时退化为网络判据 |
| `Network.requestWillBeSent` / `loadingFinished` / `loadingFailed`（事件） | 网络静默判据 | 见 `_NetworkMonitor` |
| `Page.setDocumentContent` | HTML 渲染 | 会重建渲染器，之后必须重发 metrics |
| `Page.getFrameTree` | 取主 frameId | 配合 `setDocumentContent` |
| `Page.getLayoutMetrics` | 量文档尺寸 | 优先 `cssContentSize` |
| `Runtime.evaluate` | 等图/隐藏/水印/量 innerWidth/滚动 | `_js_string()` 用 `json.dumps` 转义 |
| `Runtime.releaseObject` | 元素截图后释放 | 长跑不涨内存 |
| `DOM.enable` + `DOM.getBoxModel` | 元素 border box | 单独用会切掉阴影 |
| `DOM.getContentQuads` | 元素视觉四边形 | 与 border box 并集才是裁剪范围 |
| `Page.captureScreenshot` | 出图 | 见下面的参数约定 |
| `GET /json/version` | 等调试端口就绪、取 UA | HTTP 端点 |
| `PUT /json/new?about:blank` | 新建标签页 | 返回 `webSocketDebuggerUrl` |
| `GET /json/close/<id>` | 关标签页 | 失败忽略 |

### `Page.captureScreenshot` 参数约定

```python
{
    "format": "png",
    "captureBeyondViewport": True,   # 整页 / 元素；可视区图传 False
    "fromSurface": True,
    "optimizeForSpeed": False,
    "clip": {"x":, "y":, "width":, "height":, "scale": 1},
    "omitBackground": True,          # 仅 transparent 请求
}
```

出图高度 = `clip.height × deviceScaleFactor`，这是所有「单张上限」换算的依据。

## 5. 关键实现与踩坑记录

这一节是文档的核心：每条都是真实复现过的坑，改代码前先读。

### 5.1 整页长图里 `position:fixed` 元素跑到画面中部

`captureBeyondViewport` 只**扩大截取范围**，不改变布局视口。`fixed` 元素相对布局视口
定位，于是文档底部的固定浮层落在 `viewport_height` 处（800×600 视口 + 6000px 文档 →
浮层出现在 y=552）。

试过的错误做法：把 `fixed` 改成 `absolute` 再写死 `top`/`width`。实测像素没动，
反而把宽度写死，滚动条一出现就切掉十几像素，`sticky` 也一起被破坏。

**正解**：截图前把布局视口高度拉到目标高度（`_expand_viewport_to`），视口底 == 文档底，
fixed 元素自然落位，sticky 照常工作。分段截取时**每条都滚动到该条起点并把视口高设成
该条高度**（`window.scrollTo` + `setDeviceMetricsOverride`），fixed 元素才会只出现在图底。

### 5.2 手机预设下长图末尾整片纯白

`clip` 高度还要乘 DPR。曾经按 CSS 像素卡 20000，dpr=3 时实际要 60000px 位图，
渲染进程给不出，超出部分返回纯色空白。

**正解**：上限按 DPR 折算成 CSS 高度（`MAX_CAPTURE_HEIGHT=32000` → `_budget_for(dpr)`），
超限就分条截取再拼接。`max_dpr` 配置可以进一步在大图时自动降一档 DPR。

### 5.3 分段截取末片只剩几十像素

`range(0, height, step)` 在末尾留碎片：84016px 按 6000 切，最后一片只有 16px。
后果不只是多一张废图 —— 水印贴 `height-34`，恰好落进倒数第二条，看起来就是
「水印跑到画面中部」。

**正解**：`_plan_strips` / `plan_tiles` 先按上限定条数，再把总高**均分**，
末条高度不小于上限的一半。

### 5.4 HTML 渲染图顶部多一条乱码标题栏

`Page.setDocumentContent` 会重建渲染器并丢掉 device metrics，页面回落到 980px 的移动端
默认视口，浏览器随后画出原生标题栏（中文标题渲染成豆腐块）。

**正解**：`setDocumentContent` 之后**重发一次** `_apply_metrics` 把视口钉回来；
另外 `setDocumentContent` 不触发 load 事件，改用 `document.readyState` 轮询。

### 5.5 `omitBackground` 单独传没用

`body{background:transparent}` 的页面出图仍是 `RGB`、空白处 `(255,255,255,255)`。
只传 `omitBackground=True` 无效，必须先用
`Emulation.setDefaultBackgroundColorOverride({a:0})` 把默认背景设成全透明，
两者配合才返回 `RGBA`。

另外 `setDocumentContent` 与视口变更都会重置默认背景，所以
`_apply_metrics` 每次都补一份 `_apply_transparency`。

### 5.6 元素截图把阴影与描边整圈切掉

`DOM.getBoxModel` 给的是 border box，`box-shadow` / `outline` / 圆角光晕都在框外。
实测 300×120 带 40px 阴影 + 6px outline 的卡片，截出来阴影和描边全没。

**正解**：取 `DOM.getContentQuads` 的**全部四边形**（元素被折行或被拆成多块时会有多个），
与 border box 求并集作为裁剪范围，另加 `padding=` 让用户主动留白。

### 5.7 元素截图时水印整块消失

水印按「文档底」定位，落在元素裁剪框之外。另外水印与裁剪不能各算一次矩形，
否则边界情况必然对不上。

**正解**：抽出 `_element_rect()`，水印与 `clip` **共用同一个矩形**；
水印贴元素右下角，偏移量按元素尺寸夹取（元素很小也不会被挤出框）。

### 5.8 延迟挂载的图片被截成占位灰块

`setTimeout(() => img.src = ...)` 这类写法，旧实现只统计**调用那一刻**已在 DOM 里的图，
对「一秒后才变化」零容错。

**正解**：`_wait_images` 改成两段式 ——
① MutationObserver 在 `render_watch_ms` 窗口内盯 `<img>` 新增与 `src/srcset/style/class` 变更，
有新图就续等；② 最后统一等未解码完的图（有 1.2s 上限，不会被永不结束的图拖住）。

### 5.9 网络静默判据

旧实现是「连续 N 毫秒收不到 `loadingFinished` 就算空闲」，而该事件只在**有请求完成**时
产生：页面完全加载完、一个请求都不发的时候，事件永远不来，于是每次截图都白等一整个窗口
（实测本机页面固定等 800ms，真实加载只用了 0.03s）。

只盯 `inflight` 计数也不行：长轮询 / SSE / 埋点心跳会让它永远归不了零，实测截 cnb.cool
时 inflight 卡在 1，6 秒静默上限被整段耗光。

**正解**：`_NetworkMonitor` 以「**最近没有新的 `requestWillBeSent`**」为主判据，
inflight 只做辅助，安静 350ms 即放行，总预算受 `NETWORK_IDLE_CAP_S=6` 约束。

### 5.10 无 viewport meta 的页面在移动端下宽度失控

没有 `<meta name="viewport" content="width=device-width">` 时，移动端布局落到 980px
默认包含块（实测同一页面：无 meta 时 `innerWidth=980`、文档高 11264；有 meta 时
`innerWidth=390`、文档高 17368），而 `Emulation` 设的 390 只作用于包含块宽度。
此时整页图会比视口宽 2.5 倍，每条分段的版面也全错。

**正解**：`_document_size` 在移动端把宽度夹回 `window.innerWidth`，超宽部分横向裁掉
（与浏览器自带整页截图的行为一致）。

### 5.11 显式视口（`1280x800`）的出图会多出一倍像素

`Emulation.setDeviceMetricsOverride` 带 `deviceScaleFactor`（下称 dsf）时，
`Page.captureScreenshot` 返回的位图尺寸是 **`视口 CSS 尺寸 × dsf`**：

```
800x600 + dsf=2  ->  出图 1600x1200
800x600 + dsf=1  ->  出图  800x600
未设 override    ->  按窗口 DPR 缩放（实测默认窗口 780x437 -> 出图 780x437）
```

而本插件对 `1280x800` 这类**显式视口**统一按 DPR 2 处理（`viewport_for` 返回
`dpr=2`），于是内核又按同一个 2 放大了一次：`/截图 x.com viewport 1280x800`
出图是 **2560x1600**，比用户写的视口正好多一倍。位图比预期大 4 倍还会连带影响
单张上限换算 —— 手机预设下的长图更容易撞上限、被多切几段。

同一个「多一倍」也解释了为什么 `viewport` 模式的隐藏用例（`iphone viewport`）
期望值是 1170x2532 而不是 390x844 —— 那是既有行为，不是这次引入的。

**正解**：`_normalise_metrics_frame` 在 `viewport` 模式下把位图按同一个 dsf 缩回
CSS 尺寸，让「**输出像素 = 用户指定的视口像素**」成立。只在「缩放系数 >= 2 且是整数」
且「尺寸恰好等于 `视口×dsf`」时才动手，避免误伤「裁剪已带 scale」「dsf 非整数」的情形。
整页/元素模式不走这一步：它们的出图尺寸由 `clip` 决定，本来就带 dsf 语义。

### 5.12 空白图判定阈值

`(1-threshold)*100` 这类写法让纯色页面永远判不成空白，重试机制形同虚设。
现在用灰度 32×32 降采样后的方差（`< 12` 判空白），并把超过 2MB 的图直接排除。

### 5.13 缓存与文件名

- 文件名带 `time.time_ns()` 时间戳，同会话连续截图不会互相覆盖
- 扩展名由**文件头**推断（`detect_mime` / `suggest_suffix`），不靠格式参数
- 清理时把本次刚写出的文件列入保护名单，否则新产物会被自己删掉
- PDF 不作为图片发送，改为回「已生成文件：xxx.pdf」

### 5.14 其它

- `Page.navigate` 传 `timeout` 会污染渲染上下文（页面按旧视口布局），现在导航自身超时固定 60s
- `Page.captureScreenshot` 默认把白底烘焙进图，`fromSurface=True` + `clip` 是必须的
- 元素裁剪矩形必须夹回页面范围，负坐标会被渲染进程当成 0，图会整体错位
- `_js_string()` 必须用 `json.dumps(..., ensure_ascii=False)`，手写转义会在中文/引号上翻车
- 进程退出清理要覆盖到子进程组，否则 `terminate()` 后留僵尸 Chromium

## 6. 参数解析器

`core/config.py` 是纯函数模块，改动风险最低、收益最直接。

```
tokenize(raw)                    正则 _TOKEN_RE：优先匹配 k="v" / k='v' / k=v，再匹配引号段与裸词
  └─ _has_unbalanced_quote()     引号不配对时退化到 _tokenize_classic（按空格并拼接残缺片段）
parse_instruction(raw, defaults) 逐 token 归类：URL → 路径 → 模式关键字 → 设备 → k=v → 裸词兜底
  └─ _apply_kv()                 key 白名单映射到 ShotOptions 字段，数值走 as_int/as_float 夹取
viewport_for(device)             预设 → WxH → 回退 desktop
suggest_url(token, base)         裸域名补 https，本机地址补 http，路径原样
```

**扩展参数的四步走**：

1. `ShotOptions` 加字段（带默认值）
2. `_apply_kv` 或主循环加 key（记得同时支持中文别名）
3. `parse_instruction` 里设置默认值来源（如果配置里有对应项）
4. `main.HELP_TEXT`、`docs/USAGE.md` 参数表、`docs/DEVELOPMENT.md` 的 CDP 调用清单同步补上
5. 跑 `python tests/test_docs.py`，它会告诉你还差哪一处没写

**测试**：`tests/test_parse.py` 是解析回归，`tests/test_docs.py` 是文档一致性回归，
改了分词或参数一定两遍都跑。

## 7. 测试

### 7.1 解析回归（无需浏览器）

```bash
python3 tests/test_parse.py
```

覆盖分词、引号与转义、URL 归一、设备别名、越界夹取、模式判定等。

### 7.2 端到端实测（真 AstrBot + 真 Chromium）

```bash
# 依赖：chromium（或 Chrome）、中文字体、pip install astrbot
export ASTRBOT_ROOT=/tmp/ab
mkdir -p $ASTRBOT_ROOT/data/plugins $ASTRBOT_ROOT/data/config
ln -sfn "$PWD" $ASTRBOT_ROOT/data/plugins/astrbot_plugin_screenshot
printf '{}' > $ASTRBOT_ROOT/data/config/astrbot_plugin_screenshot_config.json
SHOT_BROWSER=$(which chromium) python3 tests/e2e_astrbot.py
```

**两个最容易踩的环境坑**（实测复现过，否则会误报「插件未能通过 AstrBot 加载」）：

- `PluginManager` 只从 `$ASTRBOT_ROOT/data/plugins` 扫插件，仓库不在那里就扫不到；
  用上面的软链接最省事
- 缺 `data/config/astrbot_plugin_screenshot_config.json` 时插件会因读配置失败而加载不了，
  空对象 `{}` 即可

跑法说明：

- `tests/e2e_astrbot.py` 用**真实的** `PluginManager` 加载插件，构造真实的
  `AstrMessageEvent` 夹具，调用真实的指令 handler，起本地 HTTP 站点当靶子，
  产物全部落盘后用 Pillow 做**像素级断言**（不是只判「有返回值」）
- 用例写在 `tests/cases.json`，可声明 `assert`：`image_size`、`min_fragments`、
  `watermark_at_bottom`、`no_pure_color_block`、`text_contains`、`transparent` 等
- 站点夹具：`site_index.html`（含长页、懒加载、阴影卡片、透明块、打印样式）、
  `site_long.html`（超长页）、`site_alpha.html`（无背景）、`site_no_viewport.html`、
  `site_sub.html`，`late_asset.png` 是延迟挂载用的图片
- 环境变量：`ASTRBOT_ROOT`（AstrBot 数据根）、`SHOT_BROWSER`（浏览器路径）、
  `SHOT_SITE_PORT`（测试站点起始端口，被占用会自动往后找）、`SHOT_SITE_DIR`

### 7.3 加一条用例的做法

1. 在 `tests/cases.json` 追加一条，写清 `label` / `handler` / `text` / `assert`
2. 需要新页面就在 `tests/site_*.html` 里加（会被自动拷到站点目录，`site_` 前缀去掉）
3. 跑一遍确认这条能在**旧代码上失败**（否则它测不出东西）
4. 修代码，再跑全量确认没有回归

### 7.4 CI

`.cnb.yml` 已配置（Pipeline 级固定 `python:3.11-slim` 镜像）：

- `main` 分支 push：`python3 -m compileall -q .` 语法检查 + `python3 tests/test_metadata.py`
- PR：语法检查 + `python3 tests/test_parse.py` 解析回归 + `python3 tests/test_docs.py` 文档一致性检查 + `python3 tests/test_metadata.py`
- tag push：自动打标签

> **踩过的坑**：默认构建镜像里只有 `python3`，没有 `python` 这个软链。
> 脚本里写 `python ...` 会以 127（command not found）直接失败，5 秒就红。
> 所以流水线脚本一律用 `python3`，并且显式指定带 Python 的镜像，
> 不依赖构建机默认 PATH。
>
> `tests/test_metadata.py` 的图像断言需要 Pillow，未装会**静默跳过**
> （打 `[SKIP] 未安装 Pillow`，退出码仍是 0）——等于新增的「缩到 64px 糊不糊」
> 这类断言白写。所以 CI 里 metadata 自检前先 `pip install -r requirements.txt`。

`tests/test_docs.py` 会核对 schema 里的配置键、`_apply_kv` 认识的参数 key 与
`main.HELP_TEXT` 是否都出现在 README / 使用文档里，改参数忘了改文档会被它拦下。

`compileall`、解析回归、文档检查都很便宜，一定要保持绿；e2e 需要真实浏览器，
留作发布前手跑。

## 8. 调试技巧

- **提日志等级**：AstrBot 日志里 `astrbot.screenshot` 是插件命名空间，
  打开 debug 能看到分段进度、网络判据、降 DPR 等决策
- **手工起一个 CDP 浏览器**对着玩：
  ```bash
  chromium --headless=new --remote-debugging-port=9222 --no-sandbox about:blank
  curl http://127.0.0.1:9222/json/version
  ```
- **只跑解析**：`python3 -c "from core.config import parse_instruction; print(parse_instruction('example.com iphone scale=2'))"`
- **看 CDP 原始报文**：临时在 `CDPConnection.send` 前后打 `method` 与 `params`
- **产物比对**：缓存目录里的 PNG 直接用图片查看器看，再和期望尺寸比对；
  e2e 的断言函数都在 `tests/e2e_astrbot.py` 里，可以单独 import 复用

## 9. 发布流程

1. 确认 `tests/test_parse.py` 与 `tests/e2e_astrbot.py` 全绿
2. 同步三处版本号与描述：`metadata.yaml`（`version`、`desc`）、`README.md`、`CHANGELOG.md`
3. 新增配置项要同步 `_conf_schema.json`（含 `description` 与 `default`）
4. 新增指令参数要同步 `main.HELP_TEXT`、`README.md` 参数表、`docs/USAGE.md`
5. 提交 PR；合入后 push tag 触发自动打标签
6. 部署环境确认已装 `chromium` 与中文字体

**版本号语义**：修渲染缺陷 / 加参数 → patch；加能力（如 PDF、透明） → minor；
破坏性改指令语法 → major。

## 10. 贡献约定

- 提交前跑 `python3 -m compileall -q .`，别让语法检查挂掉
- 每个渲染缺陷都配一条 e2e 用例，写清「旧代码为什么错、新代码为什么对」
- 代码注释写**为什么**，不写「这行在赋值」
- 中文注释、中文提示语，保持与现有代码一致
- 不在 `core/session.py` 里塞与浏览器无关的逻辑（切片、编码放 `image.py`）
