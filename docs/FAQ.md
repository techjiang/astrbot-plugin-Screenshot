# 常见问题

## 安装与部署

**Q：提示「未找到 Chromium/Chrome，可执行文件不存在」怎么办？**

装一个 Chromium：

```bash
apt-get update && apt-get install -y chromium          # Debian / Ubuntu
apk add --no-cache chromium                            # Alpine
dnf install -y chromium                                # Fedora / RHEL
```

改过 `--user-data-dir`、或浏览器在非标准路径时，在配置里填 `browser_path` 的绝对路径。
插件会自动探测 `chromium`、`chromium-browser`、`google-chrome`、`google-chrome-stable`、
`msedge`、`chrome-headless-shell` 等名字，以及 `/usr/bin`、`/snap/bin`、
`/Applications/Google Chrome.app/...` 等常见路径。

**Q：Docker 里跑，Chromium 起不来 / 起来就退出？**

- 用带 Chromium 的镜像，或在容器里装 Chromium
- 确认容器有足够 `/dev/shm`，或保留默认启动参数里的 `--disable-dev-shm-usage`
- 以 root 运行必须有 `--no-sandbox`（默认已经带上）
- 需要的话在 `launch_flags` 里追加自己的参数

**Q：插件加载报配置相关的错？**

确认 `data/config/astrbot_plugin_web_screenshot_config.json` 存在（内容为 `{}` 也行）。
手搭测试环境时最容易漏这一步。

**Q：需要装什么字体？**

至少 `fonts-noto-cjk`（中文）与 `fonts-noto-color-emoji`（emoji），
否则图里的中文和 emoji 会是方框。装完记得 `fc-cache -fv`。

## 用法

**Q：`/截图` 后面能写几个参数？会不会顺序必须固定？**

顺序无所谓，参数用空格隔开就行，可以任意组合。裸 token 会依次充当网址和 CSS 选择器。

**Q：选择器里有空格怎么写？**

三种写法都行：

```
/截图 example.com "#main > .card"
/截图 example.com selector="#main > .card"
/截图 example.com selector=#main
```

**Q：水印文本里有空格或引号怎么写？**

用引号包起来，内部引号用反斜杠转义：

```
/截图 example.com watermark="科技酱 官方"
/截图 example.com watermark="科技酱 \"官方\""
```

**Q：怎么截需要登录的页面？**

在配置 `headers` 里填 Cookie（每行一条），例如：

```
Cookie: sid=xxxxxxxx; token=yyyyyyyy
Accept-Language: zh-CN,zh;q=0.9
```

**Q：能截本地文件吗？**

能，写 `file:///path/to/page.html` 或直接写绝对路径。

**Q：能只截页面上某一块的最新内容，而不截整页吗？**

可以，用元素模式：

```
/截图 example.com "#live-data" wait=.updated
```

## 出图问题

**Q：图里的中文是方框？**

缺中文字体，见上面的安装问题。这是环境问题，不是插件问题。

**Q：页面很长，结果图被切成好几张？**

这是设计行为：聊天平台对单张图片高度有限制，超过 `max_height`（默认 6000px）会自动切片。
想少切几张可以调大 `max_height`，或让图片转 JPEG（换个 `format=jpeg`）。
超长文档建议直接 `format=pdf`。

**Q：整页图底部又出现了一次顶部导航栏？**

v0.4.0 已修。若仍出现，说明站点用 JS 在滚动时重新挂载 fixed 元素，
用 `hide=` 隐藏它，或改用 `viewport` 分段截。

**Q：长图末尾有纯白段落？**

大概率是页面用了虚拟滚动（只渲染视口内的 DOM）。可以改用 `viewport` 分段截，
或加大 `waitms=` 让内容先渲染出来。

**Q：手机预设下整页图右侧内容被裁掉了？**

页面没写 `<meta name="viewport" content="width=device-width">`，移动端布局会落到
980px 默认包含块，而实际视口只有 390px。插件把宽度夹回视口（与浏览器自带整页截图
一致），所以右侧会被裁。要完整宽度就别用移动端预设，改用 `1440x900` 之类。

**Q：截出来是纯白 / 纯黑的图？**

- 有些站点在 `document.fonts.ready` 之前是空白的 → 加 `waitms=500`
- 有些站点靠 JS 渲染内容 → 用 `wait=` 等一个真实内容选择器
- 纯黑图通常是暗色模式下的空白页 → 确认页面支持 `prefers-color-scheme`
- 插件已内置「纯色空白图自动重试」，多次重试仍白，就需要靠参数等渲染

**Q：截图很慢？**

- 首张有 1–2 秒的 Chromium 冷启动，正常
- 挂着长轮询 / SSE / 埋点心跳的页面会拖时间，可用 `timeout=` 收紧总预算
- 超长页 + 高 DPR 会分很多条，`max_dpr=2` 能让大图自动降一档 DPR
- 并发请求超过 `max_concurrent`（默认 4）会排队，可适当调大

**Q：图片被聊天平台压得看不清？**

用 `format=png`（默认）、`scale=2` 提高分辨率，或者出 `format=pdf`。

## 报错

**Q：`无法打开页面：net::ERR_NAME_NOT_RESOLVED`？**

域名解析不了。检查网址拼写；容器里 DNS 可能不通，需要配 `proxy`（支持
`http://` 与 `socks5://`，可带 `user:pass@`）。

**Q：`页面加载超时（>20s）`？**

页面一直没稳定。加大 `timeout=60000`，或放弃「等稳定」改用 `waitms=` 固定延迟。

**Q：`等待元素超时：xxx`？**

`wait=` 指定的元素一直没出现。确认选择器对不对，或加大 `timeout=`。
只想多等一会儿就用 `waitms=`。

**Q：`元素不可见或不可截取：xxx`？**

元素 `display:none`、不在渲染树里、或者被移除。先 `hide=` 掉盖在上面的浮层，
或者换一个真实可见的元素。

**Q：`选择器不合法：xxx` 和 `页面中没有找到元素：xxx` 的区别？**

前者是 CSS 语法错误（比如 `[[[`），后者是语法没错但页面上没有这个元素。
两种情况都已区分为可读提示。

## 其它

**Q：截图会联网吗？会把内容传到哪去吗？**

不会。全程本地进程内完成，只访问你指定的网址。需要出网代理时由你显式配置 `proxy`。

**Q：可以同时多个群一起截图吗？**

可以，超过 `max_concurrent` 的请求会自动排队；产出的文件名带会话 ID 与时间戳，
不会互相覆盖。

**Q：产物文件在哪里？会占满磁盘吗？**

在 `data/plugin_data/astrbot_plugin_web_screenshot/cache`，默认最多保留 120 个文件，
超出的按修改时间自动清理。插件卸载时也会清一次。
