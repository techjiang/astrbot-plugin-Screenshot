# Screenshot 使用文档

面向使用者：怎么发指令、参数怎么组合、出图不对怎么办。

- [1. 三条指令](#1-三条指令)
- [2. 参数全表](#2-参数全表)
- [3. 常见场景示例](#3-常见场景示例)
- [4. 参数解析规则](#4-参数解析规则)
- [5. 出图行为](#5-出图行为)
- [6. 报错对照表](#6-报错对照表)
- [7. 出图不对怎么查](#7-出图不对怎么查)

---

## 1. 三条指令

| 指令 | 干什么 | 用途 |
| --- | --- | --- |
| `/截图 <参数...>` | 打开网址截图 | 网页整页 / 可视区 / 元素 |
| `/元素截图 <网址> <CSS选择器>` | 同上，但强制元素模式 | 只想截某一块时语义更清楚 |
| `/渲染截图 <HTML 片段>` | 直接渲染 HTML，不走网络 | 生成通知卡、签到图、表格图 |
| `/截图帮助` | 打印用法 | 忘了参数时 |

`/截图` 不带任何参数会直接回帮助文本，不会报错。

## 2. 参数全表

参数之间用空格隔开，**顺序无关**，可以任意组合。

### 模式类

| 写法 | 说明 |
| --- | --- |
| （不写） | 整页长图（配置里 `full_page=false` 时默认可视区） |
| `full` / `fullpage` / `整页` / `长图` | 整页长图 |
| `viewport` / `screen` / `首屏` / `可视区` | 只截可视区 |
| `#main > .card`、`.cls`、`div p` | 元素截图（裸选择器，须用引号包住空格） |
| `selector=#main` / `el=` / `element=` / `选择器=` | 元素截图（显式 key 形式） |
| `print` / `打印` / `media=print` | 切到页面自带的 `@media print` 样式 |
| `render` / `html` / `渲染` | 走 HTML 渲染模式（一般直接用 `/渲染截图`） |
| `html:<片段>` | 走 HTML 渲染 |

### 设备与视口

| 写法 | 视口（CSS px） | DPR | 移动端 |
| --- | --- | --- | --- |
| `desktop` / `电脑` / `pc` | 1600×1000 | 1 | 否 |
| `laptop` / `笔记本` | 1280×800 | 2 | 否 |
| `iphone` / `手机` | 390×844 | 3 | 是 |
| `android` | 412×915 | 3 | 是 |
| `pad` / `平板` / `ipad` | 834×1112 | 2 | 是 |
| `1440x900` | 直接指定，宽高各 3–5 位数字 | 2 | 否 |

配套参数：

- `scale=2` / `2x`：在设备 DPR 之上再乘一档，最终 DPR 会被归一化到 `0.2 ~ 4`
- `device=iphone` / `viewport=iphone`：用 key 形式指定设备
- `mobile=true|false`（别名 `h5=`）：覆盖预设的移动端标记

> `1440x900` 这类**显式视口**：内部按 DPR 2 渲染以保证清晰度，出图仍是 1440×900，
> 即「输出像素 = 你写的像素」。设备预设则按预设 DPR 放大（`iphone` 390×844 出图 1170×2532）。

### 页面与等待

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| `wait=` | 选择器 | 等该元素出现在 DOM 里再截；一直等不到会报「等待元素超时」 |
| `waitms=` | 0–60000 | 固定延迟，适合「有动画/有轮询」的页面 |
| `timeout=` | 1000–600000 | 导航与等页面稳定的总预算（毫秒），默认 20000 |
| `hide=` | 选择器，逗号分隔 | 截图前 `display:none !important`；选择器写错只跳过 |
| `padding=` | 0–400 | 元素截图向外扩的留白像素，用来容下阴影 / 描边 / 圆角光晕 |
| `transparent` | 开关 | 保留透明背景，出 RGBA PNG |
| `dark` / `夜间` / `暗色` | 开关 | 模拟 `prefers-color-scheme: dark` |
| `light` / `亮色` | 开关 | 显式回到浅色（覆盖配置里的 `dark=true`） |
| `watermark=` / `mark=` | 文本 | 右下角水印 |

### 输出

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| `format=` / `图片格式=` | `png` / `jpeg` / `jpg` / `pdf` | 输出格式，默认取配置 |
| `quality=` / `q=` / `质量=` | 10–100 | JPEG 质量，默认 88 |
| `max_height=` / `maxheight=` / `切片高度=` | 0–100000 | 本次单张图最大高度；`0` = 本次不切片 |

### 其它等价写法

| 主写法 | 等价写法 |
| --- | --- |
| `scale=2` | `dpr=2`、`zoom=2` |
| `hide=.a,.b` | `remove=.a,.b` |
| `wait=.x` | `wait_for=.x` |
| `waitms=500` | `delay=500` |
| `timeout=30000` | `timeout_ms=30000` |
| `dark=true` | `theme=dark`、`theme=dark` 也认 `theme=light` |
| `selector=#a` | `el=#a`、`element=#a`、`选择器=#a` |
| `full=false` | `full_page=false` → 可视区 |
| `padding=24` | `pad=24`、`留白=24`、`边距=24` |
| `transparent=true` | `bg=transparent`、`透明`、`alpha` |

## 3. 常见场景示例

### 基础

```
/截图 https://cnb.cool/                       整页长图
/截图 https://cnb.cool/ viewport              首屏
/截图 https://cnb.cool/ "#main"               某个元素
/元素截图 https://cnb.cool/ "article .content" 元素（语义更明确）
```

### 手机效果

```
/截图 example.com 手机                         390 宽手机视口
/截图 example.com iphone scale=2 dark          手机 + 2 倍率 + 暗色
/截图 example.com 1170x2532                    直接指定高分辨率视口
```

### 页面净化（截文章、截商品页）

```
/截图 news.example.com/article hide=.ad,.float-bar,.cookie-tip
/截图 shop.example.com/item/1 "#detail" padding=16
/截图 blog.example.com/post/1 print              用站点自己的打印样式去掉导航
```

### 加标识

```
/截图 example.com watermark=科技酱
/截图 example.com watermark="科技酱 官方"         值里有空格要加引号
/截图 example.com watermark="科技酱 \"官方\""     值里要有引号就转义
```

### 等渲染（SPA / 懒加载）

```
/截图 app.example.com/spa wait=.data-table       等表格挂上去
/截图 app.example.com/spa waitms=1500            固定多等 1.5 秒
/截图 app.example.com/spa wait=.chart waitms=500 timeout=45000
```

### 输出优化

```
/截图 long.example.com/doc format=pdf           超长文档出 PDF，不被聊天平台压图
/截图 example.com format=jpeg quality=75        小体积 JPEG
/截图 example.com viewport max_height=0         本次不切片
/截图 logo.example.com transparent              透明背景 PNG
```

### HTML 渲染

```
/渲染截图 <div style="padding:24px">签到成功 ✅</div>
/渲染截图 <h1>今日汇率</h1><table>...</table> dark
/渲染截图 html:<!doctype html><html><body><p>完整文档</p></body></html>
```

片段会自动补 document 骨架、中文字体栈、图片自适应和内联代码字体；
`dark` 同时会把骨架背景换成深色。

## 4. 参数解析规则

1. **分词**：先按单双引号切，引号内的整段保留（选择器里的空格不会被拆开）。
2. **URL 识别**：带 `://`、以 `localhost` / `127.0.0.1` 开头、或形如 `example.com/path`
   的 token 视为网址；Windows 盘符和以 `/` 开头的绝对路径按 `file://` 处理。
3. **裸 token 兜底**：没被识别的第一个裸 token 当网址，第二个当 CSS 选择器。
4. **协议补全**：网址没写协议时自动补 `https://`；本机地址（`localhost`、`127.0.0.1`、
   `0.0.0.0`、`::1`、`*.local`）补 `http://`。
5. **越界夹取**：`scale=99` → `4`，`waitms=-5` → `0`，`timeout=1` → `1000`，
   不会报错也不会静默失效。
6. **非法值回退**：`quality=abc` 之类解析不了的取值回退默认值。
7. **引号转义**：引号内可用 `\"` `\'` `\\`；引号本身不配对时退化为朴素空格切分，
   尽量让指令还能用。

## 5. 出图行为

| 场景 | 行为 |
| --- | --- |
| 整页高度 ≤ 单张位图上限 | 视口拉高到文档高，一次截完 |
| 整页高度 > 单张位图上限 | 分条截取后纵向拼接成一张完整 PNG；每条都把视口底对齐该条底部，`position:fixed` 浮层只落在图底 |
| 出图高度 > `max_height` | 按片数均分切片，多张图一起发；末片不会只剩几十像素 |
| 单张仍超过体积预算 | 自动重编码为 JPEG（`transparent` 的图除外，会改用更高压缩级别的 PNG） |
| 截到纯色空白图 | 自动退避重试，最多 3 次 |
| 浏览器崩溃 / 假死 | 自动重启 Chromium 并重试一次 |
| 并发截图数超过 `max_concurrent` | 自动排队，不互相踩 |
| 缓存文件超过 120 个 | 按修改时间清理旧的；本次刚写出的产物受保护 |
| 产物是 PDF | 以「已生成文件：xxx.pdf」形式回复文件名，不作为图片发送 |

产物落在 `data/plugin_data/astrbot_plugin_screenshot/cache`，文件名形如
`<会话>_<时间戳>_<序号>.<扩展名>`，同会话连续截图不会互相覆盖。

## 6. 报错对照表

| 提示 | 原因 | 怎么办 |
| --- | --- | --- |
| `参数有误：请提供网址，例如 /截图 example.com` | 只给了参数没给网址 | 补上网址 |
| `参数有误：请提供 CSS 选择器` | 元素模式没给选择器 | 补 `#id` 或 `selector=...` |
| `截图失败：页面中没有找到元素：xxx` | 选择器语法没错但页面里没有 | 检查选择器 / 加 `wait=` 等渲染完 |
| `截图失败：选择器不合法：xxx` | CSS 语法错误 | 修选择器语法 |
| `截图失败：元素不可见或不可截取：xxx` | 元素 `display:none` / 被移除 | 换元素，或先 `hide=` 掉挡住的浮层 |
| `截图失败：元素尺寸为 0，无法截图` | 元素高度或宽度为 0 | 检查样式 |
| `截图失败：等待元素超时：xxx` | `wait=` 的元素一直没出现 | 加大 `timeout=`，或换成 `waitms=` |
| `截图失败：无法打开页面：net::ERR_NAME_NOT_RESOLVED` | 域名解析不了 | 检查网址 / 代理配置 |
| `截图失败：无法打开页面：net::ERR_CONNECTION_TIMED_OUT` | 连不上 | 检查网络与 `proxy` |
| `截图失败：页面加载超时（>20s）：xxx` | 页面一直没稳定 | 加 `timeout=60000`，或用 `waitms=` |
| `截图失败：未生成任何图片` | 出图链路异常 | 看 AstrBot 日志（插件用 `astrbot.api` 的 logger，日志级别调到 debug 可看到出图决策） |

## 7. 出图不对怎么查

**中文/emoji 变成方框**
运行环境缺字体。装 `fonts-noto-cjk`（中文）与 `fonts-noto-color-emoji`（emoji），
容器里跑的话记得 `fc-cache -fv`。

**长图底部又出现一次导航栏 / 固定浮层跑到画面中间**
已在 v0.4.0 修掉（分条截取时视口底对齐该条底部）。若仍出现，说明页面用 JS 在
滚动时重新挂载 fixed 元素，可以用 `hide=` 隐藏它，或改用 `viewport` 模式。

**页面明明很长，图却截断**
单张位图高度有上限，超限会自动分条拼接；如果图里出现纯白段落，通常是页面用了
虚拟滚动（只渲染视口内的 DOM）。这种情况建议配合 `viewport` + `waitms=` 分段截。

**手机预设下整页图比屏幕宽**
页面没写 `<meta name="viewport" content="width=device-width">`，移动端布局会落到
980px 的默认包含块。插件会把宽度夹回视口（与浏览器自带整页截图的处理一致），
所以右侧内容会被裁掉。要完整宽度就别用移动端预设。

**截图比预期慢**
- 首张有 1–2 秒冷启动，属正常
- 页面挂着长轮询 / SSE / 埋点心跳会拖长等待，可用 `timeout=` 收紧
- 超长页 + 高 DPR 会分很多条，可把 `max_dpr` 设成 2，让大图自动降一档 DPR

**图太大被平台压**
用 `format=jpeg quality=75`，或 `max_height=3000` 多切几张，超长文档直接用 `format=pdf`。

**需要登录才能看的页面**
在配置的 `headers` 里填 Cookie（每行一条，如 `Cookie: sid=xxx`），或把页面存成本地
HTML 用 `file://` 打开。
