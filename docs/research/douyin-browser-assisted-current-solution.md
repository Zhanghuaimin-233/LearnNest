# 抖音收藏 WebUI 当前方案：浏览器辅助登录

> 方案状态：已完成一次真实闭环，尚未合并产品化
>
> 快照日期：2026-07-29

## 2026-07-29 实现校准

全新隔离 Edge 不应直接构造旧的 Passport 登录入口：该入口在本机返回应用错误，而主站
在普通 Edge UA 下可以正常加载官方登录运行时。实现固定打开抖音主站并点击官方“登录”
按钮。旧 QR 兼容入口由同一隔离上下文监听官方 response 事件；默认组合验证入口不把
某一轮二维码生成或轮询结果当成整个会话的成功/失败门禁，也不手写二维码请求、轮询请求
或浏览器页面脚本。扫码二维码由同一个官方窗口呈现，避免把短信与扫码拆到不同上下文。
该调整不改变浏览器只负责登录、收藏同步仍走纯 HTTP 的边界。

第二次真实扫码暴露了此前遗漏的 Passport 二次验证分支：本次真实响应在扫码后返回
`error_code=2046`；[公开的同系 Passport 实现](https://github.com/guohuiyuan/music-lib/blob/b299302e3163765d3efcc9df592700b41867c3d8/soda/login.go#L380-L391)
将 `2046/account_flow=verify` 识别为额外验证，而不是普通业务失败。这是与现场证据吻合
的协议推断，不是抖音官方文档。已确认的本地根因是旧状态机遇到 `2046` 便立即关闭
浏览器，用户没有机会在官方窗口继续验证。当前实现已改为保持同一个隔离上下文最多
五分钟，由官方页面完成额外验证；只有上下文出现真实会话 Cookie 且收藏冒烟通过后才
进入 `connected`。该修复已有 fake 回归，尚未取得新的真实二维码复验。

## 2026-07-29 组合验证与持久化校准

官方主站登录弹窗已现场确认同时提供“验证码登录”“获取验证码”“密码登录”和扫码入口。
真实人工验收进一步确认，这些入口不是本机账号上的可替代路线：用户完成手机号验证码后，
官方仍要求手机客户端扫码确认。用户在同一个未关闭的隔离 Edge 中完成扫码后，后端才
观察到登录 Cookie。因此首选用户流程改为：在 LearnNest 启动的同一个官方隔离 Edge
窗口中，按页面要求完成手机号验证码和手机客户端扫码确认。LearnNest 不读取、转发或
保存手机号、验证码或扫码结果，只观察组合验证完成后出现的真实会话 Cookie。

用户已明确要求跨服务重启保留登录。实现使用 Windows DPAPI CurrentUser 保护完整
CookieJar，并仅把密文写入输出根目录的 `.learnnest/douyin/session.dpapi`。服务重启
时先解密，再用默认视频收藏接口严格验活；明确鉴权失败会清除密文，网络或上游临时失败
不会把旧凭据误删。该密文不能由其他 Windows 用户直接解密。

首次真实组合验证验收中，顶层 `page.goto()` 在登录控件已经可用时仍超时，旧实现因此
提前终止会话。现在顶层导航超时只记录异常类型，官方“登录”按钮可点击才是实际门禁。
修复后的全新隔离会话完成手机号验证码和手机客户端扫码确认后进入 `connected`。此前
仅根据用户最后通知的“已登录”把结果归因为手机号单独成功是不准确的；本段以用户补充的
真实操作为准。

## 目标

在 LearnNest 本地 WebUI 中完成以下用户流程：

1. 页面打开抖音官方隔离登录窗口；
2. 用户只在同一个官方窗口完成手机号验证码和手机客户端扫码确认；
3. LearnNest 获得完整登录 CookieJar；
4. CookieJar 经 Windows 当前用户加密后本地持久化；
5. 后端同步默认视频收藏；
6. WebUI 展示收藏夹、作品标题和缩略图。

## 必须说清的边界

当前方案**需要启动浏览器运行时**。

它不是登录页截图方案，也不是全程浏览器爬取方案。浏览器只负责 Passport 登录所需的
预登录 Cookie、设备状态、官方短信与扫码组合验证和 Cookie 交换；登录完成后的默认
视频收藏同步走纯 HTTP。

浏览器运行时固定为 LearnNest 管理的非持久隔离 Edge headed context；操作系统中会
出现一个 Edge 进程。实现不读取日常 Edge profile；登录成功后只把 CookieJar 的
Windows CurrentUser DPAPI 密文写入当前输出根，不持久化浏览器 profile。

## 架构

```text
LearnNest WebUI
    │
    ├── 请求登录会话
    ▼
本地 LoginSessionManager
    │
    ├── 启动专用、隔离的浏览器配置
    ├── 打开抖音主站并点击官方“登录”按钮
    └── WebUI 提示用户在官方窗口完成短信与扫码组合验证
            │
            ▼
        用户在官方窗口登录
            │
            ▼
隔离浏览器上下文出现真实会话 Cookie
    │
    ├── 在内存中导出完整 CookieJar（包含 HttpOnly）
    ├── 收藏接口严格冒烟
    └── Windows DPAPI CurrentUser 加密落盘
    ▼
DouyinHttpTransport
    │
    ├── POST 默认视频收藏接口
    ├── 解析 aweme_id、标题、封面 URL、分页游标
    └── 只投影默认视频收藏的稳定字段
            │
            ▼
本地 WebUI 收藏视图
```

## 为什么浏览器只留在登录边界

现场实测已经确认：

- 二维码生成 API 返回 Base64 PNG，不需要截图页面；
- 二维码轮询要求当前 `bdms 1.0.1.20` 产生的 `a_bogus`，官方请求还携带
  Passport 保护头和 Ticket Guard 头；这些条件尚未在普通后端运行时完整重建；
- 完整 CookieJar 加正确 `POST` 方法和表单体，可以无动态签名读取默认视频收藏；
- 只使用 `document.cookie` 会漏掉 HttpOnly Cookie，不能替代完整 CookieJar。

因此，让浏览器承载收藏同步只会增加复杂度。当前最小方案是：

```text
浏览器辅助登录 + 纯 HTTP 收藏同步
```

## 登录状态机

```text
idle
  -> starting
  -> browser_ready
  -> connected

browser_ready -> expired
任意阶段 -> failed
```

只有 `connected` 才能在 WebUI 显示“验证完成并可同步”；它要求同一隔离上下文的
完整 CookieJar 通过 `DouyinHttpTransport.list_video_favorites()` 冒烟校验。页面跳转
成功、短信提交、单次扫码状态或官方窗口关闭都不是最终成功门禁。组合流程中某一轮二维码
过期也不能关闭整个窗口，因为官方页面可能刷新二维码，用户可能仍在完成短信步骤。旧的
独立 QR 状态机仍保留兼容测试，但不再是 WebUI 默认入口。

## 本地接口草案

```text
POST /api/douyin/login/qr
POST /api/douyin/login/browser
GET  /api/douyin/login/current
GET  /api/douyin/login/{session_id}
DELETE /api/douyin/login/{session_id}
GET  /api/douyin/login/qr/{session_id}
POST /api/douyin/login/qr/{session_id}/refresh
DELETE /api/douyin/login/qr/{session_id}
POST /api/douyin/favorites
GET  /api/douyin/favorites
GET  /api/douyin/favorites/thumbnails/{relative_path}
```

- `session_id` 是本地不透明句柄，不包含抖音 token 或 Cookie。
- 取消或失败时关闭隔离上下文并清空内存凭据。

## Cookie 与本地数据

- Cookie 明文、手机号、验证码、Passport 请求参数和值不得进入 Git、SQLite、任务、
  批次或日志。
- CookieJar 只在进程内存中以明文短暂使用；持久层只允许
  `.learnnest/douyin/session.dpapi` 的 Windows CurrentUser 密文。
- 同一输出根的 WebUI、CLI、调度、下载和自动化统一先读取该 DPAPI 密文；仅当密文
  不存在时才回退到 `DOUYIN_COOKIE`。两者同时存在时密文优先，不自动写回或改写 `.env`。
- 登录态按输出根隔离；不同输出根需要各自登录，或显式使用旧环境变量回退。
- 服务重启后必须先解密并执行严格收藏冒烟，不能仅因密文存在就显示 `connected`。
- 明确鉴权失败会清除密文；临时网络失败只保持未连接，不销毁可能仍有效的本地凭据。
- 收藏事实只保存稳定 `aweme_id`、规范作品页 URL、标题、同步时间和本地缩略图路径。
- 带签名或过期参数的远程封面 URL 不作为长期事实。

## 当前代码验证

- fake Playwright 已验证官方按钮、response 事件、popup 页面、完整 CookieJar、
  `new` 等待态、`2046/account_flow=verify` 二次验证、业务失败、刷新代际、二维码
  TTL、取消、过期和 shutdown 的资源清理；
- 官方窗口登录 fake 已验证单次 QR 生成错误或轮询过期不会中断同一组合验证窗口、真实
  会话 Cookie 出现后才冒烟并持久化；fake 不能证明用户完成了哪些官方因子；
- Cookie store 已验证 DPAPI CurrentUser 真机往返、原文不可见、损坏密文 fail closed、
  原子替换、明确鉴权失败清除和 shutdown 后保留；
- 收藏 fake 已验证分页、重复 ID、严格成功状态、非 JSON、鉴权失败、封面失败、
  本地快照重启读取和缩略图路径穿越防护；
- 真实验收已完成：用户在全新隔离 Edge 中完成手机号验证码，并在同一未关闭窗口按要求
  使用手机客户端扫码确认，之后后端进入 `connected`；首次同步与服务重启后同步均返回
  130 条，标题和本地缩略图均为 `130/130`；
- 重启同一输出根后未启动 Playwright Edge，DPAPI 解密加收藏冒烟使
  `/api/douyin/login/current` 自动恢复为 `connected`；
- 运行目录明文 Cookie 标记和危险日志匹配均为 0；事实中远程签名 URL 为 0，130 个
  缩略图文件均存在且图片魔数有效，真实缩略图端点返回 `200 image/jpeg`。
- 最终组合验证复验仍为 130 条、标题 `130/130`、缩略图 `130/130`；服务重启后无
  Playwright Edge 自动恢复 `connected`。临时注入错误 `DOUYIN_COOKIE` 时，
  `_load_douyin_cookie(output_root)` 仍返回 DPAPI 凭据，证明优先级生效且未回显原文。

## 尚未验证

- 手动收藏夹目录和目录内作品的纯 HTTP 合同（不在首版范围）；
- Cookie 长期过期、多账号切换与密文迁移；
- 真实浏览器进程异常退出、登录超时和用户取消的资源清理；
- 多次长期运行下签名缩略图下载与缓存的稳定性。

## 采用条件

只有用户接受“本地会启动一个隔离浏览器运行时”时，才应实施本方案。

若硬要求是不启动任何浏览器进程，应暂停本方案，转向
[纯 API 登录实验](douyin-qr-login-api-feasibility.md)，并以手机确认后获得新 CookieJar
作为硬门禁。
