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
| `GLADOS_USER_AGENT` | ❌ | 自定义 User-Agent，需与登录设备一致（默认桌面 Chrome） |
| `GLADOS_DOMAINS` | ❌ | 目标域名，逗号分隔（默认 `glados.cloud,railgun.info`） |

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

## 输出报告格式

```
🎯 GLaDOS / Railgun 签到报告

👤 账号 1
  ✅ glados.cloud · 签到成功 · +5 积分 · 剩余 30 天 · 总 120 积分
  🔄 railgun.info · 重复签到 · 剩余 3 天 · 总 12 积分

📊 统计: 成功 1 · 重复 1 · 失败 0
──────────────────
🕒 执行时间: 2026-05-29 00:00:00
```
