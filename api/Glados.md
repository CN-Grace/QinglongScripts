# GLaDOS / Railgun 签到 API 文档

## 脚本信息

- **文件名**: `Glados.py`
- **定时任务**: `0 0 * * *` (每天午夜执行)
- **功能**: GLaDOS（`glados.cloud`）与新平台 Railgun（`railgun.info`）自动签到
- **说明**: 两个站点使用同一套 API。脚本先通过状态接口探测 Cookie 归属域名，再在该域名签到；服务端校验登录设备平台，`device-mismatch` 时自动切换 User-Agent 重试。

## 环境变量

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `GLADOS_COOKIE` | ✅ | GLaDOS / Railgun Cookie，多账号用 `&` 或换行分隔，格式 `gld:sess=...; gld:sess.sig=...` |
| `GLADOS_MAIL` | ❌ | 邮箱保底登录凭据（Cookie 全失效时用）。多账号用 `&` 或换行分隔，与 `GLADOS_COOKIE` 同序对齐 |
| `GLADOS_USER_AGENT` | ❌ | 自定义 User-Agent，需与登录设备一致（默认桌面 Chrome） |
| `GLADOS_DOMAINS` | ❌ | 目标域名，逗号分隔（默认 `glados.cloud,railgun.info`） |

> `GLADOS_COOKIE` 与 `GLADOS_MAIL` 至少填一个；两个都不填时脚本直接退出（退出码 `1`）。

> 💡 Cookie 获取：登录站点后在签到页按 `F12` → Network → 任意 `/api/user/` 请求 → 复制请求头中的完整 `Cookie`（需同时包含 `gld:sess` 与 `gld:sess.sig`）。也支持粘贴 `Cookie:` 前缀或 Cookie-Editor 的 JSON 导出。

## API 接口

> 以下接口的域名 `{domain}` 为 `glados.cloud` 或 `railgun.info`，请求头需携带 `Cookie`、`Origin: https://{domain}` 与 `User-Agent`。

### 1. 查询账号状态（用于探测 Cookie 归属域名）

**请求:**
```
GET https://{domain}/api/user/status
```

**响应:**
```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "email": "user@example.com",
    "leftDays": 30.5
  }
}
```

> Cookie 不属于该域名时返回 `code: -2`，脚本会跳过该域名。

### 2. 签到

**请求:**
```
POST https://{domain}/api/user/checkin
Content-Type: application/json;charset=UTF-8
```

**参数:**
```json
{
  "token": "{domain}"
}
```

**响应:**
```json
{
  "code": 0,
  "message": "Checkin! Got 5 Points",
  "points": 5
}
```

**返回码:**

| code | 含义 | 处理 |
|:---:|:---|:---|
| `0` | 签到成功 | 记录本次获得积分 |
| `1` | 重复签到 | 视为正常，不再重试 |
| `-2` | 无权限 | Cookie 失效或不属于该域名 |
| `4` | 风控 / 设备不匹配 | `reason=device-mismatch` 时按 `loginDevice` 切换 UA 重试一次 |

### 3. 查询总积分

**请求:**
```
GET https://{domain}/api/user/points
```

**响应:**
```json
{
  "code": 0,
  "points": 120
}
```

### 4. 积分兑换（脚本默认不调用）

**请求:**
```
POST https://{domain}/api/user/exchange
```

**参数:**
```json
{
  "planType": "plan500"
}
```

**响应:**
```json
{
  "code": 0,
  "message": "兑换成功"
}
```

### 5. 请求邮箱验证码（仅保底登录使用）

**请求:**
```
POST https://{domain}/api/authorization
Content-Type: application/json
```

**参数:**
```json
{
  "address": "user@example.com",
  "site": "glados.network"
}
```

> `site` 是**平台标识而非域名**，两站取值不同：`glados.cloud` → `glados.network`，`railgun.info` → `railgun.info`。`address` 需小写（与前端 `trim().toLowerCase()` 一致）。

**响应:**
```json
{
  "code": 0,
  "message": "access code sent. please check mailbox",
  "method": "sendgrid"
}
```

**异常返回:**

| 响应 | 含义 | 脚本处理 |
|:---|:---|:---|
| `{"code": 0, "captcha_required": true, "sitekey": "0x..."}` | 站点要求人机验证 | 标记 `🧩 captcha`，换下一个域名再试 |
| `{"code": -6}` 或 `message` 为 `registration_closed` | 站点已关闭邮箱验证码注册/登录 | 标记 `🚪 closed`，**终止**保底 |
| `message` 含 `migrated` | 账号已迁移（glados.cloud → Railgun） | 标记 `🔀 migrated`，换下一个域名再试 |
| `{"code": -1, "message": "Failed to send email"}` | 站点临时停用邮箱验证码（维护中） | 标记 `🛠️ unavailable`，换下一个域名再试 |
| 其它非 `0` | 邮箱地址错误 / 被拒 | 标记 `⚠️ error` |

### 6. 邮箱验证码登录（仅保底登录使用）

**请求:**
```
POST https://{domain}/api/login
Content-Type: application/json
```

**参数:**
```json
{
  "method": "email",
  "site": "glados.network",
  "email": "user@example.com",
  "mailcode": "182514"
}
```

**响应:**
```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "userId": 42
  }
}
```

登录成功后服务端通过 `Set-Cookie` 下发新的 `gld:sess` / `gld:sess.sig`，前端只把 `userId` 写进 `localStorage`。脚本直接复用该 `Session` 的 Cookie 罐，**不再手动拼 Cookie 头**，确保 `status` / `checkin` 与 `login` 属于同一会话、同一 User-Agent（否则会触发 `device-mismatch`）。

失败时同样按上表的 `registration_closed` / `migrated` 规则分类。

## 邮箱验证码保底登录

当所有 Cookie（环境变量 + 本地缓存）都被判定失效时，脚本自动走邮箱验证码登录，拿到新 Cookie 后继续签到。

**流程:**

```
1. 用候选 Cookie 逐个域名探测 /api/user/status
2. 全部无效且配置了 GLADOS_MAIL
3. 记录邮箱水位快照（IMAP 最大 UID / POP3 邮件数）
4. POST /api/authorization 请求发码
5. 轮询新邮件（最长 120s，每 10s 一次），提取验证码
6. POST /api/login 完成登录，得到新 Cookie
7. 用同一 Session 直接签到，并把新 Cookie 写入 config/.glados_cookie
```

> 第 3 步是防误用的关键：只在**发码之后**到达的新邮件里找验证码，避免拿历史邮件里的旧码去登录。

### `GLADOS_MAIL` 格式

**短格式**（服务商可识别时最省事）：
```
邮箱----授权码
```

**长格式**（自建域名或需显式指定接入点）：
```
邮箱----授权码----host----port----protocol----ssl
```

- `protocol` 为 `imap` / `pop3`，`ssl` 为 `true` / `false`
- 多账号用 `&` 或换行分隔，**顺序需与 `GLADOS_COOKIE` 一致**（第 N 个 Cookie 对应第 N 个邮箱）
- Gmail 应用专用密码中的空格会被自动去除
- 授权码（而非登录密码）请到邮箱设置里生成，并确保已开启 IMAP/POP3 服务

**内置服务商默认接入点**（与脚本内 `MAIL_PROVIDERS` 逐条对应）:

| 邮箱后缀 | IMAP | POP3 |
|:---|:---|:---|
| `qq.com` · `vip.qq.com` · `foxmail.com` | `imap.qq.com:993` | `pop.qq.com:995` |
| `163.com` | `imap.163.com:993` | `pop.163.com:995` |
| `126.com` | `imap.126.com:993` | `pop.126.com:995` |
| `yeah.net` | `imap.yeah.net:993` | `pop.yeah.net:995` |
| `gmail.com` · `googlemail.com` | `imap.gmail.com:993` | `pop.gmail.com:995` |
| `outlook.com` · `hotmail.com` · `live.com` | `outlook.office365.com:993` | `outlook.office365.com:995` |
| `icloud.com` | `imap.mail.me.com:993` | `pop.mail.me.com:995` |

未收录的服务商必须用长格式指定 `host`，否则该条目会被跳过并告警。

### 验证码邮件识别

两个平台是**不同品牌**，发件方与标题都不同，脚本对两者都认：

| 平台 | 发件方 | 标题 |
|---|---|---|
| `glados.cloud` | `GLaDOS <noreply@glados.network>` | `GLaDOS Authentication` / `GLaDOS Authentication Code` |
| `railgun.info` | `Railgun <noreply@railgun.info>` | `Railgun Authentication` |

匹配条件是「发件方含 `glados` 或 `railgun`」+「标题含 `authentication`」两条。标题这条不能省：站内通知邮件（如 `GLaDOS 服务到期提醒(13天)`、`[GLaDOS]用户注册1周年优惠活动`）同样由官方品牌发出且正文带数字，只按发件方匹配会误抓。

验证码正文形态随模板演进，脚本按优先级三级提取：

| 优先级 | 形态 | 说明 |
|---|---|---|
| 1 | `Verification Code: 610521` / `Your verification code is: 610521` | 显式标签，标签文字不会出现在邮箱地址里，误判率最低 |
| 2 | `*182514*` | 旧模板的星号包裹形态 |
| 3 | 特征句 `You are receiving this email because you are requesting a ... authentication code` 之后的独立数字 | 前两级都没命中时的兜底 |

> 现行验证码是 **5–6 位**数字（实测 `38788` / `610521`），星号包裹只在旧模板出现。以 2026-11 邮箱留存的 16 封真实邮件回归验证：6 封新模板走标签命中、2 封旧模板走星号命中、8 封站内通知全部正确拒绝。

其它容错：

- 正文按 `\s+` 拍平后再匹配，避免标签/特征句被 78 列硬换行截断
- 支持 `base64` / `quoted-printable` / `8bit` 传输编码、`gbk` 等非 UTF-8 字符集、`multipart/alternative`、RFC 2047 编码邮件头
- 收件人地址里的数字会被排除，避免误取账号名中的数字
- 请求发码时带上当前平台品牌，同时到达多封验证码邮件时优先采纳品牌一致的那封（品牌不符的仅在无更好选择时使用，并告警）
- 轮询超时的原因会报清「水位之后来了 N 封新邮件但都没匹配上（sender-unmatched / subject-unmatched）」，而不是笼统的「没收到」

### Cookie 缓存

新 Cookie 写入 `config/.glados_cookie`（JSON，按邮箱索引，`config/` 已在 `.gitignore` 中）。下次执行时缓存 Cookie 会作为候选参与探测，排在环境变量 Cookie 之后 —— 即「先试手工 Cookie，再用自动登录存下的」。

### 限制与风险

- `glados.cloud` 的 `/api/authorization` 目前固定返回 `captcha_required`（Cloudflare Turnstile，实测 `sitekey=0x4AAAAAAFF5pkT0bg7fX7nw`），脚本无法自动完成，会**自动换** `railgun.info` 重试 —— 这是设计内的正常路径，不是故障，也不会消耗验证码邮件
- 请求发码与登录都是**非幂等写操作**，脚本**不挂重试**（登录用裸 `requests.Session()`，仅签到等读接口保留 Retry），失败即如实报状态
- 邮箱授权码等价于账号凭据，请只放在环境变量或本地配置中，不要提交到仓库
- 日志与通知中的邮箱地址一律脱敏为 `gr***@gmail.com`

## 输出报告格式

```
🎯 GLaDOS / Railgun 签到报告

👤 账号 1
  ✅ glados.cloud · 签到成功 · +5 积分 · 剩余 30 天 · 总 120 积分
  🔄 railgun.info · 重复签到 · 剩余 3 天 · 总 12 积分

👤 账号 2（gr***@gmail.com）
  🔑 邮箱保底: 邮箱验证码登录成功，新 Cookie 已写入 config/.glados_cookie
  ✅ railgun.info · 签到成功 · +5 积分 · 剩余 28 天 · 总 130 积分

📊 统计: 成功 2 · 重复 1 · 失败 0
──────────────────
🕒 执行时间: 2026-05-29 00:00:00
```

**退出码:** `0` = 无失败账号；`1` = 存在失败账号或未配置任何凭据。
