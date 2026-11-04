# NodeSeek 签到 API 文档

## 脚本信息

- **文件名**: `Nodeseek.py`
- **定时任务**: `0 0 * * *`（每天午夜执行）
- **功能**: NodeSeek 论坛自动签到，Cookie 失效时可用邮箱验证码保底登录

## 环境变量

| 变量名 | 必填 | 默认 | 说明 |
|--------|------|------|------|
| `NS_COOKIE` | ⚠️ 见下 | — | 站点 Cookie。值是 `session=...; pjwt=...; smac=...` 的原始串 |
| `NS_RANDOM` | ❌ | `true` | 签到前随机延迟 0–5 分钟；`false` 立即执行 |
| `NS_IMPERSONATE` | ❌ | `chrome110` | curl_cffi 的浏览器指纹，用于过 Cloudflare 的 TLS 指纹检测 |
| `NS_MAIL` | ❌ | — | 保底登录用。短格式 `邮箱----IMAP授权码`，长格式 `邮箱----授权码----imap.example.com` |
| `NS_CAPTCHA_KEY` | ❌ | — | 保底登录用。YesCaptcha 的 `ClientKey` |
| `NS_CAPTCHA_BASE` | ❌ | `https://cn.yescaptcha.com` | 解盾服务地址（海外可用 `https://api.yescaptcha.com`） |

`NS_COOKIE` 与「`NS_MAIL` + `NS_CAPTCHA_KEY`」至少要有一套，否则脚本直接报错退出。

## 站点接口

### 1. 签到

```
POST https://www.nodeseek.com/api/attendance?random=true|false
Content-Type: application/json
Cookie: {NS_COOKIE}
```

请求体为空 JSON `{}`。判定：

| 响应 | 处理 |
|---|---|
| `message` 含 `鸡腿`，或 `success` 为真 | 签到成功 |
| `message` 含 `已完成签到` | 今日已签到，视为正常 |
| `status == 404` 或 `message` 含 `USER NOT FOUND` | **Cookie 失效**，触发保底登录 |

> ⚠️ NodeSeek 由 Cloudflare 防护。无效 Cookie 打 API 会收到 `403 Just a moment...` 挑战页，因此脚本用 `curl_cffi` 的 `impersonate` 复现真实浏览器的 TLS/JA3 指纹。注意 `impersonate` **只能**解决 TLS 指纹，解决不了 Turnstile 和 JS 指纹。

### 2. 请求邮箱验证码（保底登录）

```
POST https://www.nodeseek.com/api/email
Content-Type: application/json

{"email": "...", "mode": "totp", "token": "<turnstile token>", "source": "turnstile", "version": "v3"}
```

**这道门有 Cloudflare Turnstile 服务端强校验** — 必须带一个真实解出的 token，否则返回：

```json
{"success": false, "message": "Cloudflare人机验证未通过"}
```

站点 sitekey 是公开的（在前端 bundle 里）：`0x4AAAAAAAaNy7leGjewpVyR`（生产）/ `0x4AAAAAACYfzKxNfR026Cpk`（仅 localhost）。

### 3. 登录

```
POST https://www.nodeseek.com/api/account/emailSignIn
Content-Type: application/json

{"email": "...", "code": "<邮箱验证码>"}
```

成功返回 `{"success": true}`，并通过 `Set-Cookie` 下发会话三件套：`session`（HttpOnly）、`pjwt`、`smac`。

**两处实测结论（与前端代码的推测不同）**：

1. 前端会带 `x-integrity-token`（FingerprintJS 设备指纹哈希）和 `x-security-token`，但**服务端并不校验** —— 实测不带任何 token 头也能登录成功。所以纯 HTTP 可行。
2. 验证码不是 6 位数字，而是 **24 位十六进制字符串**（如 `1444e6a4e41814a2e07c9917`）。

## 邮箱验证码保底登录

Cookie 全部失效（环境变量里的 + 本地缓存的）且配置了 `NS_MAIL` + `NS_CAPTCHA_KEY` 时自动触发：

```
1. IMAP 登录邮箱，记录当前最大 UID 作为水位
2. 调解盾服务解 Turnstile  -> token（约 10s，扣 25 点）
3. POST /api/email 发码
4. 轮询新邮件（最长 120s / 每 8s），只接受水位之后到达的
5. POST /api/account/emailSignIn 登录，收集 Set-Cookie
6. 新 Cookie 写入 config/.nodeseek_cookie，直接复用它签到
```

### 验证码邮件识别

| 项 | 值 |
|---|---|
| 发件方 | 含 `nodeseek` |
| 标题 | `邮箱动态验证登录`（不含 nodeseek 字样，故按「含 `验证`」匹配） |
| 正文 | `nodeseek邮箱动态验证登录，你的验证码是1444e6a4e41814a2e07c9917，不要告诉他人` |
| 提取 | 特征句 `你的验证码是` 之后的第一串 16–64 位十六进制 |

防误抓：发件方 + 标题双重限定；正文先按 `\s+` 拍平再匹配（邮件普遍 78 列硬换行）。

### Cookie 缓存

新 Cookie 写入 `config/.nodeseek_cookie`（JSON，`config/` 已在 `.gitignore` 中）。下次执行时候选顺序是「环境变量 `NS_COOKIE` → 缓存 Cookie」，前者失效才用后者，都失效才走保底。

## 限制与风险

- **解盾服务质量与 IP 强相关。** Turnstile 的 token 与产出环境绑定，机房 IP 出示时可能被判无效。住宅 IP 实测可通过，VPS 需自行验证。
- **每次保底登录消耗解盾点数**（Turnstile 约 25 点）。YesCaptcha 新用户有 1500 点免费额度，够用很久。
- **发码与登录是非幂等写操作，脚本不盲目重试。** 仅对「请求根本没拿到响应」的连接层故障（SSL/超时）做退避重试，避免重复发码扣点。
- **有账号风险。** 用解盾服务绕过站点的反自动化措施可能违反 NodeSeek 条款，存在封号风险。请自行评估后再启用 `NS_CAPTCHA_KEY`；不配置该变量则保底功能完全关闭。
- 邮箱授权码等价于账号凭据，只放在环境变量或本地配置中，不要提交到仓库。
- 日志与通知中的邮箱地址一律脱敏为 `qq***@gmail.com`。

## 输出报告格式

```
✅ 状态: already
⏱️ 耗时: 19436 ms
🕯️ 延迟: 0s (签到前随机延迟)
📝 说明: 今天已完成签到，请勿重复操作

──────────────────
🕒 执行时间: 2026-11-04 19:51:27
```

触发保底时首行会多一条：

```
🔑 邮箱保底: 邮箱验证码登录成功，新 Cookie 已写入 config\.nodeseek_cookie
```
