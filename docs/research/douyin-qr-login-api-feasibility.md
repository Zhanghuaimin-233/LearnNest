# 抖音二维码登录 API 可行性实测

> 状态：研究结论，不代表已实现的产品能力
>
> 实测日期：2026-07-28 至 2026-07-29
>
> 范围：二维码生成、扫码状态轮询、登录 Cookie 交换边界，以及登录后收藏夹 HTTP 路线

当前可实施但需要浏览器运行时的方案单独记录在
[抖音收藏 WebUI 当前方案：浏览器辅助登录](douyin-browser-assisted-current-solution.md)。

## 结论

直接 API 路线值得继续，WebUI 不需要截取抖音登录页截图。不过截至本快照，**纯 API
方案尚未通过轮询门禁**；可实施基线仍是在 LearnNest 管理的隔离浏览器上下文中运行
Passport 登录，再把完整 CookieJar 交给纯 HTTP 收藏同步。

- 二维码生成接口可以由 LearnNest 后端直接调用；它不要求登录 Cookie，也不要求
  `a_bogus` 或 `msToken`。
- 二维码状态轮询接口需要当前 Passport 请求上下文。实测中 `a_bogus` 是必要参数，
  `msToken` 不是必要参数。当前页面声明的签名版本为 `p_bd=1.0.1.20`。
- `a_bogus` 不是一次性绑定：一个生成约 263 秒后的轮询签名仍能用于另一个新二维码
  token 的 `new` 状态查询。不过本轮没有证明它可以稳定存活数小时或数天，因此不能写成
  “有效期很久”。
- 两套公开的纯 Python `a_bogus` 生成器都无法通过当前轮询校验：针对新生成的二维码，
  服务端均返回 `error_code=4031`。其中较新的 F2 实现仍标注 `1.0.1.19`，不能视为
  当前 `1.0.1.20` 的兼容实现。
- 官方 `bdms.js` 在真实页面中能够初始化；把同一脚本放入普通 Node 运行时并只提供最小
  浏览器对象替身时，会在其自定义字节码 VM 内因缺少可调用函数而中止。这说明还需要重建
  浏览器能力或提炼当前算法，不代表纯 API 路线在原理上不可能。
- 扫码确认阶段会校验生成端和轮询端的一致性。用最小参数生成二维码、再借用另一浏览器
  上下文轮询时，未扫码阶段能返回 `new`，手机确认后却返回 `error_code=2156`，不能完成
  Cookie 交换。
- 即使在同一个后端 CookieJar 中复用同一浏览器捕获的完整生成/轮询查询上下文，手机确认
  后仍返回 `2156`。后续网络检查发现，该重放没有复制官方请求携带的 Passport 保护头和
  Ticket Guard 头。因此先前把失败归因于“预登录 Cookie 或设备状态”的说法过宽；这些
  保护头是尚未排除的具体差异，`2156` 的单一根因仍未收敛。
- 登录后的默认视频收藏接口已现场验证纯 HTTP：正确请求是带完整 CookieJar 的 `POST`，
  分页参数位于表单请求体中，不需要动态签名。把浏览器性能日志中的 URL 当作 `GET`
  重放并返回 HTML，不能证明该接口必须依赖浏览器动态签名。

因此，当前最小根因不是“抖音登录后所有资源仍有很强保护”，而是：

1. 普通后端尚不能生成通过当前 `bdms 1.0.1.20` 校验的轮询签名；纯 API 流程在扫码前
   就被 `4031` 拦截。
2. 已进入 `new` 的浏览器参数重放又遗漏了官方 Passport/Ticket Guard 保护头，所以
   `2156` 还不能证明必须保留整个浏览器设备状态。
3. 先前对收藏接口的失败重放没有复刻真实 HTTP 方法和请求体。

这里的工程结论是“当前实现受阻”，不是“纯 API 不可能”。

## 纯 API 的硬验收口径

只有同时满足以下条件，才算纯 API 登录方案成功：

1. 二维码生成、轮询、手机确认和 Cookie 交换都在普通 HTTP/JavaScript 运行时完成；
2. 运行期间不存在 Chromium、Edge、WebView2 等浏览器进程；
3. 不读取浏览器 Cookie、CDP 状态或临时复制浏览器产生的签名参数；
4. 新 CookieJar 能通过默认收藏接口冒烟，并取得结构正确的作品条目。

浏览器可以用于观察官方协议和对照请求，但它产生的签名或状态不能计入纯 API 成功证据。

## 二维码生成接口

```text
GET https://login.douyin.com/passport/web/get_qrcode/
```

实测可工作的最小查询参数：

| 参数 | 实测值或用途 |
| --- | --- |
| `passport_jssdk_version` | `3.2.0` |
| `passport_jssdk_type` | `normal` |
| `is_from_ttaccountsdk` | `1` |
| `aid` | `6383` |
| `next` | `https://www.douyin.com` |
| `need_short_url` | `true` |
| `need_logo` | `false` |

响应为 HTTP 200 JSON，`data.error_code=0`，并包含：

- `qrcode`：不带 Data URI 前缀的 Base64 PNG；
- `token`：后续轮询所需的临时 token；
- `expire_time`：Unix 时间戳；
- `qrcode_index_url`：抖音 App 扫码入口。

本轮多次测得 `expire_time - 当前时间 = 60 秒`。WebUI 必须展示倒计时，并在过期后自动
生成新二维码。

只传 `aid`、`next`、`need_short_url` 和 `need_logo` 会返回
`data.error_code=4031`；补齐三个 Passport SDK 身份参数后即可成功。补齐后移除
`a_bogus` 和 `msToken` 仍然成功。

## 二维码轮询接口

```text
POST https://login.douyin.com/passport/web/check_qrconnect/
```

表单请求体包含：

| 参数 | 用途 |
| --- | --- |
| `token` | 生成接口返回的临时 token |
| `need_logo` | `false` |
| `is_frontier` | `true` |
| `is_new_login` | `1` |
| `next` | `https://www.douyin.com` |
| `need_short_url` | `true` |

官方前端识别的状态包括：

| 状态 | 含义 |
| --- | --- |
| `new` / `1` | 等待扫码 |
| `scanned` / `2` | 已扫码，等待手机确认 |
| `confirmed` / `3` | 已确认，进入登录结果交换 |
| `refused`、`expired` / `4`、`5` | 拒绝或过期 |

轮询查询参数的最小边界尚未完全收敛，但本轮已经确认：

- 使用浏览器产生的完整查询上下文：返回 `error_code=0`、`status=new`；
- 只移除 `msToken`：仍返回 `new`；
- 只移除 `a_bogus`：返回 `error_code=4031`；
- 同时移除两者：返回 `error_code=4031`；
- 约 263 秒前产生的完整查询上下文仍能轮询另一个新 token。

这只能证明 `a_bogus` 至少可在数分钟内复用，不能证明其长期有效。

## 2026-07-29 纯 API 签名实验

本轮对每套生成器都采用全新二维码 token，并在普通 Python 运行时生成轮询查询参数：

| 生成路线 | 结果 | 证据边界 |
| --- | --- | --- |
| Evil0ctal 的旧版纯 Python `ABogus` | HTTP 200，`error_code=4031` | 最小查询和浏览器字段形状均失败 |
| F2 的纯 Python `ABogus` | HTTP 200，`error_code=4031` | 实现注释仍对应 `1.0.1.19` |
| 官方 `bdms 1.0.1.20` + 最小 Node 浏览器替身 | 初始化中止 | 自定义 VM 内出现 `undefined is not a function` |

当前浏览器产生的有效轮询查询中，`a_bogus` 长度为 180；两套公开实现生成的字符串形状
和当前服务端接受的实现并不等价。长度差异本身不是判定依据，最终依据是服务端对新请求
返回 `4031`。

这组实验与“浏览器捕获的签名在 263 秒后仍可复用”不矛盾：前者验证的是**能否在纯
后端生成新签名**，后者只给出了**某个已经有效的签名可复用至少 263 秒**的下界。

参考实现：

- [F2 当前 `abogus.py`](https://github.com/Johnserf-Seed/f2/blob/main/f2/utils/abogus.py)
- [Evil0ctal 旧版 `abogus.py`](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/main/crawlers/douyin/web/abogus.py)
- [TikTokDownloader 当前项目说明](https://github.com/JoeanAmier/TikTokDownloader)

## Passport 保护头与 Ticket Guard

官方登录页的请求还存在以下仅记录名称和形状的保护头：

| 请求 | 保护头 |
| --- | --- |
| `get_qrcode` | `x-tt-passport-csrf-token`、`x-tt-passport-trace-id`、`x-tt-passport-verify-portrait`、`x-tt-session-dtrait` |
| `check_qrconnect` | 上述 Passport 头，以及 `bd-ticket-guard-ree-public-key`、`bd-ticket-guard-version`、`bd-ticket-guard-web-sign-type`、`bd-ticket-guard-web-version` |

官方 Secure SDK 使用 P-256 运行时密钥；`bd-ticket-guard-ree-public-key` 是 88 字符的
Base64 未压缩公钥。普通 Node/WebCrypto 可以生成同形状的公钥，因此“产生客户端密钥”
本身不是浏览器硬依赖。但完整流程还包括服务端数据、ECDH/HKDF 和后续路径签名，本轮尚未
在纯 API 会话中完成握手。

先前后端重放只复制了查询参数和 CookieJar，没有复制这些头。由于当前纯生成的
`a_bogus` 先在轮询阶段得到 `4031`，本轮没有让用户重复扫码，也没有把 Ticket Guard
对 `confirmed` 的影响单独验证出来。

## 扫码确认与 `2156`

一次混合上下文实验采用：

1. 最小参数直接生成二维码；
2. 使用另一浏览器登录页的 `fp`、`verifyFp`、Passport 风控字段与 `a_bogus` 轮询；
3. 未扫码时正常返回 `new`；
4. 手机确认后返回 `error_code=2156`，没有得到 `confirmed`。

2026-07-29 又进行了更严格的对照：

1. 从同一官方登录页捕获完整 `get_qrcode` 和 `check_qrconnect` 查询上下文；
2. 在同一个全新 PowerShell `WebRequestSession` 中生成二维码并持续轮询；
3. 未扫码时稳定返回 `new`；
4. 用户扫码并在手机确认后返回 `error_code=2156`，描述为“系统繁忙，请重启应用或刷新
   页面后重试”。

因此，“相同 URL 查询上下文 + 相同后端 CookieJar”仍不足以完成确认。但该请求还遗漏了
上节列出的保护头，所以不能再把 `2156` 单独解释为预登录 Cookie 或设备状态问题。当前
只足以说明：复放 URL 查询参数不是完整的官方请求。

这说明未扫码轮询不能作为端到端成功标准。真正的成功标准必须是：

1. 收到 `confirmed`；
2. 使用同一个内存 CookieJar 请求响应中的登录跳转；
3. CookieJar 获得抖音登录 Cookie；
4. 用该 CookieJar 对收藏接口执行一次正确的 `POST` 冒烟；
5. 得到 JSON、`status_code=0` 和至少一个结构正确的收藏条目。

同一连接的 Edge 随后从官方登录页跳转到 `https://www.douyin.com/jingxuan`，页面无登录
提示，完整 CookieJar 中存在 `sessionid`、`sessionid_ss`、`sid_guard`、`sid_tt` 与
`passport_csrf_token`，无签名收藏请求也通过全部资源门禁。不过该 Edge 配置在本轮之前
已有抖音登录记录，因此不能把这次跳转当作“全新隔离配置首次扫码交换 Cookie”已经通过。

## 登录后收藏接口的重新判断

默认视频收藏接口：

```text
POST https://www.douyin.com/aweme/v1/web/aweme/listcollection/?aid=6383
Content-Type: application/x-www-form-urlencoded

count=<page-size>&cursor=<cursor>
```

公开仓当前 `DouyinHttpTransport.list_video_favorites()` 已按这一合同实现无签名基线。
本轮又用当前登录态做了两次对照：

1. 只使用页面 `document.cookie` 可见的 60 个 Cookie，请求返回 HTTP 200、
   `text/plain` 和 7 字节非 JSON。该集合缺少 HttpOnly Cookie。
2. 通过 CDP `Storage.getCookies` 在内存中取得完整 CookieJar，筛出 79 条有效抖音
   Cookie（74 个唯一名称）后执行相同请求，得到 HTTP 200 JSON、31,676 字节、
   `status_code=0`、一个收藏条目及 `has_more=1`。
3. 将页大小改为 3 后，三个条目均包含 `aweme_id`、非空标题和封面 URL 列表；三个
   封面列表也都带过期或签名查询参数。一次脱离页面下载首张封面的探测超时，因此本轮只
   确认字段可用，尚未确认后端缩略图缓存下载的稳定性。

测试没有输出或落盘任何 Cookie 值。结果证明默认视频收藏不需要 `a_bogus`，但必须保留
完整 CookieJar，不能把 `document.cookie` 当作完整登录凭据。

因此，先前“复制浏览器中的完整 URL 后执行 `fetch` 得到 HTML”只说明那次重放方式不对，
不能推导出 Cookie 之后仍必须截获浏览器响应。

后续端到端登录成功后，应优先验证现有纯 HTTP transport，而不是先引入 CDP 响应截获。
手动收藏夹接口仍应独立验证，不能把默认收藏接口的结论自动外推到所有接口。

## 推荐的 WebUI 路线

### 后端状态机

```text
idle
  -> qr_ready
  -> scanned
  -> confirmed
  -> cookie_exchanged
  -> favorites_verified

qr_ready/scanned -> expired -> qr_ready
任意阶段 -> failed
```

### 本地 API 草案

```text
POST /api/douyin/login/qr
GET  /api/douyin/login/qr/{session_id}
POST /api/douyin/login/qr/{session_id}/refresh
POST /api/douyin/favorites/sync
```

- `session_id` 只是本地不透明句柄，WebUI 不接触抖音 token 或 Cookie。
- token 与 Passport 请求上下文只存在于 LearnNest 管理的隔离浏览器上下文；Cookie
  通过运行时边界交给纯 HTTP transport，不写入任务或数据库。
- 二维码可以直接使用 API 返回的 PNG，不需要浏览器截图。
- 过期后后端生成新 token，前端替换图片并重置 60 秒倒计时。
- 只有 `favorites_verified` 才向用户显示“登录成功并可同步”。

### 仍需解决的实现点

1. 使用 LearnNest 专用、隔离且可重启的浏览器配置运行官方 Passport SDK；在该上下文内
   调用二维码 API，并把返回的 Base64 PNG 转发给 WebUI。它不是登录页截图。
2. 在全新隔离配置中完成首次 `confirmed`、登录跳转和 Cookie 导出，避免把用户日常 Edge
   的既有登录态误算成扫码成功。
3. 登录 Cookie 交换后复用已经现场验证的默认视频收藏 `POST` 纯 HTTP 冒烟。
4. 分别验证手动收藏夹目录与目录内作品接口。
5. Cookie 不写入 Git、SQLite、日志、任务或批次文件。若未来需要跨重启保存，必须另行
   设计 Windows 用户级加密存储并获得明确授权。

## 当前证据边界

- 已证明：直接 API 生成二维码、Base64 PNG 展示、60 秒 TTL、轮询 `new`/`scanned`、
  `a_bogus` 的必要性与至少数分钟复用。
- 已证明：两套公开纯 Python 签名器都不能通过当前 `p_bd=1.0.1.20` 的轮询校验，均返回
  `4031`；官方 `bdms.js` 不能靠最小浏览器对象替身直接搬进 Node。
- 已证明：官方二维码请求携带 Passport 保护头，轮询请求还携带 Ticket Guard 头；先前
  `2156` 重放没有包含这些头。
- 已证明：完整 CookieJar 加正确 `POST` 方法和表单请求体，可以在无动态签名条件下读取
  默认视频收藏 JSON，并取得标题与封面 URL；只使用 `document.cookie` 的可见子集不够。
- 已证明：混用生成/轮询风控上下文会在手机确认后失败。
- 已证明：只复用同一组浏览器查询参数和后端 CookieJar、但遗漏官方保护头时，会在手机
  确认后返回 `2156`。
- 已证明：当前连接的 Edge 登录页可跳转到已登录页面，完整 CookieJar 含登录 Cookie，
  并能直接读取收藏；但该浏览器配置存在历史登录状态。
- 未证明：全新隔离浏览器配置首次扫码后成功收到 `confirmed`、导出新 Cookie，并在重启
  后保持预期登录状态。
- 未证明：`a_bogus` 可稳定复用数小时或数天。
- 未证明：普通后端可以生成当前 `1.0.1.20` 的有效 `a_bogus`。
- 未证明：补齐 Ticket Guard 完整握手后，纯 API 会话可以从 `confirmed` 交换到新
  CookieJar。
- 未证明：所有手动收藏夹接口都能像默认视频收藏一样无签名调用。
- 未证明：后端在不借助浏览器渲染的情况下可以稳定下载所有签名缩略图。
