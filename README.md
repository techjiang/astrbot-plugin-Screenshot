<div align="center">

<img src="assets/logo.png" alt="Screenshot · AstrBot Plugin" width="240">

# Screenshot

新一代**非 API 接口**的 AstrBot 截图插件。

它不调用 Playwright、Selenium、html2image 这类浏览器封装库，
而是直接说 Chrome DevTools Protocol（CDP）——用 WebSocket 把
`Page.navigate` / `Page.captureScreenshot` / `Emulation.setDeviceMetricsOverride`
这些浏览器内核原生命令发下去。

</div>

## 为什么是「非 API 接口」

| 传统做法 | 问题 | 本插件的做法 |
| --- | --- | --- |
| Playwright / Selenium | 需要匹配浏览器与驱动版本，升级即崩 | 只依赖 CDP 协议，浏览器大版本升级照常工作 |
| html2image / wkhtmltoimage | 自带一套旧内核，CSS 支持落后 | 用的是同一个 Chromium，渲染结果与真机一致 |
| 各类截图 API 服务 | 需要外网、要 key、有配额、有隐私风险 | 全程本地进程内完成，不出网、无配额 |

CDP 是浏览器自带的调试协议，只要进程带着 `--remote-debugging-port` 起来就能用。
插件只用到命令行 + HTTP + WebSocket 三样东西，没有版本漂移。
`requirements.txt` 里只有 `aiohttp` 与 `Pillow`。

## 安装

把仓库放进 AstrBot 的插件目录，或直接在插件市场安装：

```
astrbot/data/plugins/astrbot_plugin_screenshot/
```

插件需要系统里存在一个 Chromium/Chrome。Debian/Ubuntu 上：

```bash
apt-get update && apt-get install -y chromium fonts-noto-cjk fonts-noto-color-emoji
```

Docker 部署时，使用自带 Chromium 的镜像，或在容器内安装上述包。
插件会按 `chromium` → `chromium-browser` → `google-chrome` → `msedge`
以及常见安装路径的顺序自动寻找，找不到时才需要在配置里显式填 `browser_path`。

## 用法

```
/截图 <网址>                    整页长图
/截图 <网址> viewport           只截可视区
/截图 <网址> #main .card        截指定元素（等价于 /元素截图）
/截图 <网址> iphone scale=2     指定设备与缩放倍率
/截图 <网址> dark               暗色模式
/截图 <网址> hide=.ad,.float    截图前隐藏干扰元素
/截图 <网址> watermark=@bot     右下角打水印
/截图 <网址> wait=.loaded       等元素出现后再截
/截图 <网址> format=jpeg        输出 JPEG（更小）
/截图 <网址> format=pdf         输出 PDF（超长页面首选）
/元素截图 <网址> <CSS选择器>    元素截图
/渲染截图 <h1>你好</h1>         直接渲染 HTML 片段
/截图帮助                       查看用法
```

参数可以任意组合、顺序无关。裸 token 会依次充当网址与选择器，
`/截图 example.com "#main > .card"` 与 `/截图 example.com selector=#main` 等价。

### 参数表

| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `full` / `viewport` | 整页 / 可视区 | 整页 |
| `desktop` `laptop` `iphone` `android` `pad` | 设备预设，也认 `手机` `平板` `pc` | `desktop` |
| `1440x900` | 直接指定视口尺寸 | — |
| `scale=2` / `2x` | 在设备 DPR 之上再放大（上限 4 倍） | `1` |
| `dark=true` / `light` | 模拟 `prefers-color-scheme` | `false` |
| `mobile=true` | 覆盖设备预设的移动端标记 | 跟随预设 |
| `selector=#id` | 元素截图选择器 | — |
| `wait=.cls` | 等待该选择器出现 | — |
| `waitms=800` | 固定延迟毫秒 | `0` |
| `hide=.a,.b` | 截图前 `display:none` 的逗号分隔选择器 | — |
| `watermark=文本` | 固定定位水印 | — |
| `timeout=30000` | 页面导航超时毫秒 | `20000` |
| `format=png\|jpeg\|pdf` | 输出格式 | `png` |
| `quality=80` | JPEG 质量 | `88` |
| `max_height=12000` | 本次截图的切片高度 | 跟随配置 |

### 渲染 HTML

`/渲染截图` 接受 HTML 片段，插件会自动补上 `<!doctype html>` 骨架、
中文字体栈与图片自适应样式；如果你贴的是完整文档，则原样渲染。

```
/渲染截图 <div style="padding:24px">签到成功 ✅</div>
/渲染截图 html:<!doctype html><html><body>...</body></html>
```

## 配置

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `browser_path` | string | Chromium 可执行文件绝对路径，留空自动探测 |
| `launch_flags` | list | 追加到启动命令的参数 |
| `proxy` | string | 出网代理，支持 `http://` 与 `socks5://`，可带 `user:pass@` |
| `headers` | text | 附加请求头，每行一条，形如 `Cookie: a=b` |
| `max_concurrent` | int | 并发截图上限，超出自动排队 |
| `max_height` | int | 单张图片最大高度，超出自动切片，`0` 表示不切 |
| `image_format` | string | 默认输出格式 `png` / `jpeg` |
| `jpeg_quality` | int | JPEG 质量 |
| `device` | string | 默认设备 |
| `full_page` | bool | 默认是否整页 |
| `dark` | bool | 默认是否暗色 |
| `timeout_ms` | int | 默认导航超时 |

## 行为说明

- **整页图过高会自动切片**：默认 6000px 一段，切片后仍超体积预算时重编码为 JPEG，
  避免聊天平台拒收；`format=pdf` 则不做切片，直接折成多页 PDF。
- **浏览器进程常驻**：插件加载时启动一次 Chromium，每次截图新开一个标签页、用完即关，
  插件卸载时连整个进程组一起回收；进程假死会自动重启后重试一次。
- **等渲染有三级保险**：`load` 事件 → 网络静默 → 图片解码完成；
  截到纯色空白图会自动重试（最多 3 次，指数退避）。
- **缓存自动清理**：输出落在插件数据目录的 `cache/`，最多保留 120 张。
- **首次使用有冷启动**：约 1–2 秒，之后单次截图通常在 1 秒内。

## 目录结构

```
main.py              插件入口与指令注册
core/browser.py      Chromium 进程管理 + CDP 连接收发
core/session.py      导航、等待渲染、四类截图模式
core/config.py       指令串解析与参数归一
core/image.py        长图切片、重编码、PDF 封装、HTML 包装
```

## 效果

整页长图与 HTML 渲染的实测输出见 `assets/`。

## 已验证

在 AstrBot 4.14.6 + Chromium 153（headless）下逐项实测：

- 整页长图、可视区、元素截图、HTML 渲染、PDF 输出五类模式均产出正确文件
- `iphone` 预设 × `scale` × `watermark` × `hide` × `wait` × `format` 组合参数生效
- 超过 6000px 的长图按预期切片，超体积自动转 JPEG
- 指令参数由 AstrBot 的 `GreedyStr` 完整透传，含引号与空格的 HTML 片段不被切碎

## 关于作者

| 渠道 | 地址 |
| --- | --- |
| 作者 | 科技酱 |
| 作者网站 | <https://docs.asoe.cn> |
| GitHub | <https://github.com/techjiang/> |
| 哔哩哔哩 | <https://space.bilibili.com/1768832152> |
| 玲珑社区 | <https://forums.asoe.cn/> |
| QQ 群 | 291974598 |
| QQ 群② | 474819022 |

## License

MIT
